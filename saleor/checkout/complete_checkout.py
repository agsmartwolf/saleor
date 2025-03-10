from datetime import timedelta
from decimal import Decimal
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
    Union,
    cast,
)
from uuid import UUID

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError
from django.db import transaction
from django.forms.models import model_to_dict
from django.utils import timezone
from prices import Money, TaxedMoney

from ..account.error_codes import AccountErrorCode
from ..account.models import User
from ..account.utils import retrieve_user_by_email, store_user_address
from ..channel import MarkAsPaidStrategy
from ..checkout import CheckoutAuthorizeStatus, calculations
from ..checkout.error_codes import CheckoutErrorCode
from ..core.exceptions import GiftCardNotApplicable, InsufficientStock
from ..core.postgres import FlatConcatSearchVector
from ..core.taxes import TaxError, zero_taxed_money
from ..core.tracing import traced_atomic_transaction
from ..core.transactions import transaction_with_commit_on_errors
from ..core.utils.url import validate_storefront_url
from ..discount import DiscountType, DiscountValueType
from ..discount.models import NotApplicable, OrderLineDiscount
from ..discount.utils import (
    add_voucher_usage_by_customer,
    get_sale_id,
    increase_voucher_usage,
    prepare_promotion_discount_reason,
    release_voucher_usage,
)
from ..graphql.checkout.utils import (
    prepare_insufficient_stock_checkout_validation_error,
)
from ..order import OrderOrigin, OrderStatus
from ..order.actions import mark_order_as_paid_with_payment, order_created
from ..order.fetch import OrderInfo, OrderLineInfo
from ..order.models import Order, OrderLine
from ..order.notifications import send_order_confirmation
from ..order.search import prepare_order_search_vector_value
from ..order.utils import (
    update_order_authorize_data,
    update_order_charge_data,
    update_order_display_gross_prices,
)
from ..payment import PaymentError, TransactionKind, gateway
from ..payment.models import Payment, Transaction
from ..payment.utils import fetch_customer_id, store_customer_id
from ..product.models import ProductTranslation, ProductVariantTranslation
from ..tax.utils import (
    get_shipping_tax_class_kwargs_for_order,
    get_tax_class_kwargs_for_order_line,
)
from ..warehouse.availability import check_stock_and_preorder_quantity_bulk
from ..warehouse.management import allocate_preorders, allocate_stocks
from ..warehouse.models import Reservation, Stock
from ..warehouse.reservations import is_reservation_enabled
from . import AddressType
from .base_calculations import (
    base_checkout_delivery_price,
    calculate_base_line_unit_price,
    calculate_undiscounted_base_line_total_price,
    calculate_undiscounted_base_line_unit_price,
)
from .calculations import fetch_checkout_data
from .checkout_cleaner import (
    _validate_gift_cards,
    clean_billing_address,
    clean_checkout_payment,
    clean_checkout_shipping,
)
from .fetch import (
    CheckoutInfo,
    CheckoutLineInfo,
    fetch_checkout_info,
    fetch_checkout_lines,
)
from .models import Checkout
from .utils import (
    get_checkout_metadata,
    get_or_create_checkout_metadata,
    get_voucher_for_checkout_info,
)

if TYPE_CHECKING:
    from ..app.models import App
    from ..plugins.manager import PluginsManager
    from ..site.models import SiteSettings


def _process_voucher_data_for_order(checkout_info: "CheckoutInfo") -> dict:
    """Fetch, process and return voucher/discount data from checkout.

    Careful! It should be called inside a transaction.
    If voucher has a usage limit, it will be increased!

    :raises NotApplicable: When the voucher is not applicable in the current checkout.
    """
    checkout = checkout_info.checkout
    voucher = get_voucher_for_checkout_info(checkout_info, with_lock=True)

    if checkout.voucher_code and not voucher:
        msg = "Voucher expired in meantime. Order placement aborted."
        raise NotApplicable(msg)

    if not voucher:
        return {}

    if voucher.usage_limit:
        increase_voucher_usage(voucher)
    if voucher.apply_once_per_customer:
        customer_email = cast(str, checkout_info.get_customer_email())
        add_voucher_usage_by_customer(voucher, customer_email)
    return {
        "voucher": voucher,
    }


def _process_shipping_data_for_order(
    checkout_info: "CheckoutInfo",
    base_shipping_price: Money,
    shipping_price: TaxedMoney,
    manager: "PluginsManager",
    lines: Iterable["CheckoutLineInfo"],
) -> Dict[str, Any]:
    """Fetch, process and return shipping data from checkout."""
    delivery_method_info = checkout_info.delivery_method_info
    shipping_address = delivery_method_info.shipping_address

    if (
        delivery_method_info.store_as_customer_address
        and checkout_info.user
        and shipping_address
    ):
        store_user_address(
            checkout_info.user, shipping_address, AddressType.SHIPPING, manager=manager
        )
        if checkout_info.user.addresses.filter(pk=shipping_address.pk).exists():
            shipping_address = shipping_address.get_copy()

    shipping_method = delivery_method_info.delivery_method
    tax_class = getattr(shipping_method, "tax_class", None)

    result: Dict[str, Any] = {
        "shipping_address": shipping_address,
        "base_shipping_price": base_shipping_price,
        "shipping_price": shipping_price,
        "weight": checkout_info.checkout.get_total_weight(lines),
        **get_shipping_tax_class_kwargs_for_order(tax_class),
    }
    result.update(delivery_method_info.delivery_method_order_field)
    result.update(delivery_method_info.delivery_method_name)

    return result


def _process_user_data_for_order(checkout_info: "CheckoutInfo", manager):
    """Fetch, process and return shipping data from checkout."""
    billing_address = checkout_info.billing_address

    if checkout_info.user and billing_address:
        store_user_address(
            checkout_info.user, billing_address, AddressType.BILLING, manager=manager
        )
        if checkout_info.user.addresses.filter(pk=billing_address.pk).exists():
            billing_address = billing_address.get_copy()

    return {
        "user": checkout_info.user,
        "user_email": checkout_info.get_customer_email(),
        "billing_address": billing_address,
        "customer_note": checkout_info.checkout.note,
    }


def _create_line_for_order(
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    checkout_line_info: "CheckoutLineInfo",
    products_translation: Dict[int, Optional[str]],
    variants_translation: Dict[int, Optional[str]],
    prices_entered_with_tax: bool,
) -> OrderLineInfo:
    """Create a line for the given order.

    :raises InsufficientStock: when there is not enough items in stock for this variant.
    """
    checkout_line = checkout_line_info.line
    quantity = checkout_line.quantity
    variant = checkout_line_info.variant
    product = checkout_line_info.product

    product_name = str(product)
    variant_name = str(variant)

    translated_product_name = products_translation.get(product.id, "")
    translated_variant_name = variants_translation.get(variant.id, "")

    if translated_product_name == product_name:
        translated_product_name = ""

    if translated_variant_name == variant_name:
        translated_variant_name = ""

    # the price with sale and discounts applied - base price that is used for
    # total price calculation
    base_unit_price = calculate_base_line_unit_price(
        line_info=checkout_line_info, channel=checkout_info.channel
    )
    # the unit price before applying any discount (sale or voucher)
    undiscounted_base_unit_price = calculate_undiscounted_base_line_unit_price(
        line_info=checkout_line_info,
        channel=checkout_info.channel,
    )
    # the total price before applying any discount (sale or voucher)
    undiscounted_base_total_price = calculate_undiscounted_base_line_total_price(
        line_info=checkout_line_info,
        channel=checkout_info.channel,
    )
    undiscounted_unit_price = TaxedMoney(
        net=undiscounted_base_unit_price, gross=undiscounted_base_unit_price
    )
    undiscounted_total_price = TaxedMoney(
        net=undiscounted_base_total_price, gross=undiscounted_base_total_price
    )
    # total price after applying all discounts - sales and vouchers
    total_line_price = calculations.checkout_line_total(
        manager=manager,
        checkout_info=checkout_info,
        lines=lines,
        checkout_line_info=checkout_line_info,
    )
    # unit price after applying all discounts - sales and vouchers
    unit_price = calculations.checkout_line_unit_price(
        manager=manager,
        checkout_info=checkout_info,
        lines=lines,
        checkout_line_info=checkout_line_info,
    )
    tax_rate = calculations.checkout_line_tax_rate(
        manager=manager,
        checkout_info=checkout_info,
        lines=lines,
        checkout_line_info=checkout_line_info,
    )

    voucher_code = None
    if checkout_line_info.voucher:
        voucher_code = checkout_line_info.voucher.code

    discount_price = undiscounted_unit_price - unit_price
    if prices_entered_with_tax:
        discount_amount = discount_price.gross
    else:
        discount_amount = discount_price.net

    unit_discount_reason = None
    if voucher_code:
        unit_discount_reason = f"Voucher code: {voucher_code}"

    tax_class = None
    if product.tax_class_id:
        tax_class = product.tax_class
    else:
        tax_class = product.product_type.tax_class

    line = OrderLine(  # type: ignore[misc] # see below:
        product_name=product_name,
        variant_name=variant_name,
        translated_product_name=translated_product_name,
        translated_variant_name=translated_variant_name,
        product_sku=variant.sku,
        product_variant_id=variant.get_global_id(),
        is_shipping_required=variant.is_shipping_required(),
        is_gift_card=variant.is_gift_card(),
        quantity=quantity,
        variant=variant,
        unit_price=unit_price,  # money field not supported by mypy_django_plugin
        undiscounted_unit_price=undiscounted_unit_price,  # money field not supported by mypy_django_plugin # noqa: E501
        undiscounted_total_price=undiscounted_total_price,  # money field not supported by mypy_django_plugin # noqa: E501
        total_price=total_line_price,
        tax_rate=tax_rate,
        voucher_code=voucher_code,
        unit_discount=discount_amount,  # money field not supported by mypy_django_plugin # noqa: E501
        unit_discount_reason=unit_discount_reason,
        unit_discount_value=discount_amount.amount,  # we store value as fixed discount
        base_unit_price=base_unit_price,  # money field not supported by mypy_django_plugin # noqa: E501
        undiscounted_base_unit_price=undiscounted_base_unit_price,  # money field not supported by mypy_django_plugin # noqa: E501
        metadata=checkout_line.metadata,
        private_metadata=checkout_line.private_metadata,
        **get_tax_class_kwargs_for_order_line(tax_class),
    )

    line_discounts = _create_order_line_discounts(checkout_line_info, line)
    if line_discounts:
        # Currently only one promotion can be applied on the single line.
        # This is temporary solution until the discount API is implemented.
        # Ultimately, this info should be taken from the orderLineDiscount instances.

        promotion = checkout_line_info.rules_info[0].promotion
        sale_id = get_sale_id(promotion)
        line.sale_id = sale_id
        promotion_discount_reason = prepare_promotion_discount_reason(
            promotion, sale_id
        )
        unit_discount_reason = (
            f"{unit_discount_reason} & {promotion_discount_reason}"
            if unit_discount_reason
            else promotion_discount_reason
        )
        line.unit_discount_reason = unit_discount_reason

    is_digital = line.is_digital
    line_info = OrderLineInfo(
        line=line,
        quantity=quantity,
        is_digital=is_digital,
        variant=variant,
        digital_content=variant.digital_content if is_digital and variant else None,
        warehouse_pk=checkout_info.delivery_method_info.warehouse_pk,
        line_discounts=line_discounts,
    )

    return line_info


def _create_order_line_discounts(
    checkout_line_info: "CheckoutLineInfo", order_line: "OrderLine"
) -> List["OrderLineDiscount"]:
    line_discounts = []
    discounts = checkout_line_info.get_promotion_discounts()
    for discount in discounts:
        discount_data = model_to_dict(discount)
        discount_data.pop("line")
        discount_data["promotion_rule_id"] = discount_data.pop("promotion_rule")
        discount_data["line_id"] = order_line.pk
        line_discounts.append(OrderLineDiscount(**discount_data))
    return line_discounts


def _create_lines_for_order(
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    prices_entered_with_tax: bool,
) -> Iterable[OrderLineInfo]:
    """Create a lines for the given order.

    :raises InsufficientStock: when there is not enough items in stock for this variant.
    """
    translation_language_code = checkout_info.checkout.language_code
    country_code = checkout_info.get_country()

    variants = []
    quantities = []
    products = []
    for line_info in lines:
        variants.append(line_info.variant)
        quantities.append(line_info.line.quantity)
        products.append(line_info.product)

    products_translation = ProductTranslation.objects.filter(
        product__in=products, language_code=translation_language_code
    ).values("product_id", "name")
    product_translations = {
        product_translation["product_id"]: product_translation.get("name")
        for product_translation in products_translation
    }

    variants_translation = ProductVariantTranslation.objects.filter(
        product_variant__in=variants, language_code=translation_language_code
    ).values("product_variant_id", "name")
    variants_translation = {
        variant_translation["product_variant_id"]: variant_translation.get("name")
        for variant_translation in variants_translation
    }

    additional_warehouse_lookup = (
        checkout_info.delivery_method_info.get_warehouse_filter_lookup()
    )
    check_stock_and_preorder_quantity_bulk(
        variants,
        country_code,
        quantities,
        checkout_info.channel.slug,
        global_quantity_limit=None,
        delivery_method_info=checkout_info.delivery_method_info,
        additional_filter_lookup=additional_warehouse_lookup,
        existing_lines=lines,
        replace=True,
        check_reservations=True,
    )
    return [
        _create_line_for_order(
            manager,
            checkout_info,
            lines,
            checkout_line_info,
            product_translations,
            variants_translation,
            prices_entered_with_tax,
        )
        for checkout_line_info in lines
    ]


def _prepare_order_data(
    *,
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    prices_entered_with_tax: bool,
) -> dict:
    """Run checks and return all the data from a given checkout to create an order.

    :raises NotApplicable InsufficientStock:
    """
    checkout = checkout_info.checkout
    order_data = {}
    address = (
        checkout_info.shipping_address or checkout_info.billing_address
    )  # FIXME: check which address we need here

    taxed_total = calculations.calculate_checkout_total_with_gift_cards(
        manager=manager,
        checkout_info=checkout_info,
        lines=lines,
        address=address,
    )

    base_shipping_price = base_checkout_delivery_price(checkout_info, lines)
    shipping_total = calculations.checkout_shipping_price(
        manager=manager,
        checkout_info=checkout_info,
        lines=lines,
        address=address,
    )
    shipping_tax_rate = calculations.checkout_shipping_tax_rate(
        manager=manager,
        checkout_info=checkout_info,
        lines=lines,
        address=address,
    )
    order_data.update(
        _process_shipping_data_for_order(
            checkout_info, base_shipping_price, shipping_total, manager, lines
        )
    )
    order_data.update(_process_user_data_for_order(checkout_info, manager))

    order_data["lines"] = _create_lines_for_order(
        manager,
        checkout_info,
        lines,
        prices_entered_with_tax,
    )
    undiscounted_total = (
        sum(
            [line.line.undiscounted_total_price for line in order_data["lines"]],
            start=zero_taxed_money(taxed_total.currency),
        )
        + shipping_total
    )

    order_data.update(
        {
            "language_code": checkout.language_code,
            "tracking_client_id": checkout.tracking_code or "",
            "total": taxed_total,
            "undiscounted_total": undiscounted_total,
            "shipping_tax_rate": shipping_tax_rate,
        }
    )

    # validate checkout gift cards
    _validate_gift_cards(checkout)

    order_data.update(_process_voucher_data_for_order(checkout_info))

    order_data["total_price_left"] = (
        calculations.checkout_subtotal(
            manager=manager,
            checkout_info=checkout_info,
            lines=lines,
            address=address,
        )
        + shipping_total
        - checkout.discount
    ).gross

    try:
        manager.preprocess_order_creation(checkout_info, lines)
    except TaxError:
        release_voucher_usage(order_data.get("voucher"), order_data.get("user_email"))
        raise

    return order_data


@traced_atomic_transaction()
def _create_order(
    *,
    checkout_info: "CheckoutInfo",
    checkout_lines: Iterable["CheckoutLineInfo"],
    order_data: dict,
    user: User,
    app: Optional["App"],
    manager: "PluginsManager",
    site_settings: Optional["SiteSettings"] = None,
    metadata_list: Optional[List] = None,
    private_metadata_list: Optional[List] = None,
) -> Order:
    """Create an order from the checkout.

    Each order will get a private copy of both the billing and the shipping
    address (if shipping).

    If any of the addresses is new and the user is logged in the address
    will also get saved to that user's address book.

    Current user's language is saved in the order so we can later determine
    which language to use when sending email.
    """
    from ..order.utils import add_gift_cards_to_order

    checkout = checkout_info.checkout
    order = Order.objects.filter(checkout_token=checkout.token).first()
    if order is not None:
        return order

    total_price_left = order_data.pop("total_price_left")
    order_lines_info = order_data.pop("lines")

    if site_settings is None:
        site_settings = Site.objects.get_current().settings

    status = (
        OrderStatus.UNFULFILLED
        if checkout_info.channel.automatically_confirm_all_new_orders
        else OrderStatus.UNCONFIRMED
    )
    order = Order.objects.create(
        **order_data,
        checkout_token=str(checkout.token),
        status=status,
        origin=OrderOrigin.CHECKOUT,
        channel=checkout_info.channel,
        should_refresh_prices=False,
        tax_exemption=checkout_info.checkout.tax_exemption,
    )

    _handle_checkout_discount(order, checkout)

    order_lines: List[OrderLine] = []
    order_line_discounts: List[OrderLineDiscount] = []
    for line_info in order_lines_info:
        line = line_info.line
        line.order_id = order.pk
        order_lines.append(line)
        if discounts := line_info.line_discounts:
            order_line_discounts.extend(discounts)

    OrderLine.objects.bulk_create(order_lines)
    OrderLineDiscount.objects.bulk_create(order_line_discounts)

    country_code = checkout_info.get_country()
    additional_warehouse_lookup = (
        checkout_info.delivery_method_info.get_warehouse_filter_lookup()
    )
    allocate_stocks(
        order_lines_info,
        country_code,
        checkout_info.channel,
        manager,
        checkout_info.delivery_method_info.warehouse_pk,
        additional_warehouse_lookup,
        check_reservations=True,
        checkout_lines=[line.line for line in checkout_lines],
    )
    allocate_preorders(
        order_lines_info,
        checkout_info.channel.slug,
        check_reservations=is_reservation_enabled(site_settings),
        checkout_lines=[line.line for line in checkout_lines],
    )

    add_gift_cards_to_order(checkout_info, order, total_price_left, user, app)

    # assign checkout payments to the order
    checkout.payments.update(order=order)
    checkout_metadata = get_checkout_metadata(checkout)

    # store current tax configuration
    update_order_display_gross_prices(order)

    # copy metadata from the checkout into the new order
    order.metadata = checkout_metadata.metadata
    if metadata_list:
        order.store_value_in_metadata({data.key: data.value for data in metadata_list})

    order.redirect_url = checkout.redirect_url

    order.private_metadata = checkout_metadata.private_metadata
    if private_metadata_list:
        order.store_value_in_private_metadata(
            {data.key: data.value for data in private_metadata_list}
        )

    update_order_charge_data(order, with_save=False)
    update_order_authorize_data(order, with_save=False)
    order.search_vector = FlatConcatSearchVector(
        *prepare_order_search_vector_value(order)
    )
    order.save()

    order_info = OrderInfo(
        order=order,
        customer_email=order_data["user_email"],
        channel=checkout_info.channel,
        payment=order.get_last_payment(),
        lines_data=order_lines_info,
    )

    transaction.on_commit(
        lambda: order_created(
            order_info=order_info,
            user=user,
            app=app,
            manager=manager,
            site_settings=site_settings,
        )
    )

    # Send the order confirmation email
    transaction.on_commit(
        lambda: send_order_confirmation(order_info, checkout.redirect_url, manager)
    )

    return order


def _prepare_checkout(
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    redirect_url,
):
    """Prepare checkout object to complete the checkout process."""
    checkout = checkout_info.checkout
    clean_checkout_shipping(checkout_info, lines, CheckoutErrorCode)
    if not checkout_info.channel.is_active:
        raise ValidationError(
            {
                "channel": ValidationError(
                    "Cannot complete checkout with inactive channel.",
                    code=CheckoutErrorCode.CHANNEL_INACTIVE.value,
                )
            }
        )
    if redirect_url:
        try:
            validate_storefront_url(redirect_url)
        except ValidationError as error:
            raise ValidationError(
                {"redirect_url": error}, code=AccountErrorCode.INVALID.value
            )

    to_update = []
    if redirect_url and redirect_url != checkout.redirect_url:
        checkout.redirect_url = redirect_url
        to_update.append("redirect_url")

    if to_update:
        to_update.append("last_change")
        checkout.save(update_fields=to_update)


def _prepare_checkout_with_transactions(
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    redirect_url: Optional[str],
):
    """Prepare checkout object with transactions to complete the checkout process."""
    clean_billing_address(checkout_info, CheckoutErrorCode)
    if (
        checkout_info.checkout.authorize_status != CheckoutAuthorizeStatus.FULL
        and not checkout_info.channel.allow_unpaid_orders
    ):
        raise ValidationError(
            {
                "id": ValidationError(
                    "The authorized amount doesn't cover the checkout's total amount.",
                    code=CheckoutErrorCode.CHECKOUT_NOT_FULLY_PAID.value,
                )
            }
        )
    if checkout_info.checkout.voucher_code and not checkout_info.voucher:
        raise ValidationError(
            {
                "voucher_code": ValidationError(
                    "Voucher not applicable",
                    code=CheckoutErrorCode.VOUCHER_NOT_APPLICABLE.value,
                )
            }
        )
    _validate_gift_cards(checkout_info.checkout)
    _prepare_checkout(
        manager=manager,
        checkout_info=checkout_info,
        lines=lines,
        redirect_url=redirect_url,
    )
    try:
        manager.preprocess_order_creation(checkout_info, lines)
    except TaxError as tax_error:
        raise ValidationError(
            f"Unable to calculate taxes - {str(tax_error)}",
            code=CheckoutErrorCode.TAX_ERROR.value,
        )


def _prepare_checkout_with_payment(
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    redirect_url: Optional[str],
    payment: Optional[Payment],
):
    """Prepare checkout object with payment to complete the checkout process."""
    clean_checkout_payment(
        manager,
        checkout_info,
        lines,
        CheckoutErrorCode,
        last_payment=payment,
    )
    _prepare_checkout(
        manager=manager,
        checkout_info=checkout_info,
        lines=lines,
        redirect_url=redirect_url,
    )


def _get_order_data(
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    site_settings: "SiteSettings",
) -> dict:
    """Prepare data that will be converted to order and its lines."""
    tax_configuration = checkout_info.tax_configuration
    prices_entered_with_tax = tax_configuration.prices_entered_with_tax
    try:
        order_data = _prepare_order_data(
            manager=manager,
            checkout_info=checkout_info,
            lines=lines,
            prices_entered_with_tax=prices_entered_with_tax,
        )
    except InsufficientStock as e:
        error = prepare_insufficient_stock_checkout_validation_error(e)
        raise error
    except NotApplicable:
        raise ValidationError(
            "Voucher not applicable",
            code=CheckoutErrorCode.VOUCHER_NOT_APPLICABLE.value,
        )
    except GiftCardNotApplicable as e:
        raise ValidationError(e.message, code=e.code)
    except TaxError as tax_error:
        raise ValidationError(
            f"Unable to calculate taxes - {str(tax_error)}",
            code=CheckoutErrorCode.TAX_ERROR.value,
        )
    return order_data


def _process_payment(
    payment: Payment,
    customer_id: Optional[str],
    store_source: bool,
    payment_data: Optional[dict],
    order_data: dict,
    manager: "PluginsManager",
    channel_slug: str,
) -> Transaction:
    """Process the payment assigned to checkout."""
    try:
        if payment.to_confirm:
            txn = gateway.confirm(
                payment,
                manager,
                additional_data=payment_data,
                channel_slug=channel_slug,
            )
        else:
            txn = gateway.process_payment(
                payment=payment,
                token=payment.token,
                manager=manager,
                customer_id=customer_id,
                store_source=store_source,
                additional_data=payment_data,
                channel_slug=channel_slug,
            )

        payment.refresh_from_db()
        if not txn.is_success:
            raise PaymentError(txn.error)
    except PaymentError as e:
        release_voucher_usage(order_data.get("voucher"), order_data.get("user_email"))
        raise ValidationError(str(e), code=CheckoutErrorCode.PAYMENT_ERROR.value)
    return txn


def complete_checkout_pre_payment_part(
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    user,
    site_settings=None,
    redirect_url=None,
) -> Tuple[Optional[Payment], Optional[str], dict]:
    """Logic required to process checkout before payment.

    Should be used with transaction_with_commit_on_errors, as there is a possibility
    for thread race.
    :raises ValidationError
    """
    if site_settings is None:
        site_settings = Site.objects.get_current().settings

    fetch_checkout_data(checkout_info, manager, lines)

    checkout = checkout_info.checkout
    channel_slug = checkout_info.channel.slug
    payment = checkout.get_last_active_payment()
    try:
        _prepare_checkout_with_payment(
            manager=manager,
            checkout_info=checkout_info,
            lines=lines,
            redirect_url=redirect_url,
            payment=payment,
        )
    except ValidationError as exc:
        gateway.payment_refund_or_void(payment, manager, channel_slug=channel_slug)
        raise exc

    try:
        order_data = _get_order_data(manager, checkout_info, lines, site_settings)
    except ValidationError as exc:
        gateway.payment_refund_or_void(payment, manager, channel_slug=channel_slug)
        raise exc

    customer_id = None
    if payment and user:
        customer_id = fetch_customer_id(user=user, gateway=payment.gateway)

    return payment, customer_id, order_data


def complete_checkout_post_payment_part(
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    payment: Optional[Payment],
    txn: Optional[Transaction],
    order_data,
    user,
    app,
    site_settings=None,
    metadata_list: Optional[List] = None,
    private_metadata_list: Optional[List] = None,
) -> Tuple[Optional[Order], bool, dict]:
    action_required = False
    action_data: Dict[str, str] = {}

    if payment and txn:
        if txn.customer_id and user:
            store_customer_id(user, payment.gateway, txn.customer_id)

        action_required = txn.action_required
        if action_required:
            action_data = txn.action_required_data
            release_voucher_usage(
                order_data.get("voucher"), order_data.get("user_email")
            )

    order = None
    if not action_required and not _is_refund_ongoing(payment):
        try:
            order = _create_order(
                checkout_info=checkout_info,
                checkout_lines=lines,
                order_data=order_data,
                user=user,
                app=app,
                manager=manager,
                site_settings=site_settings,
                metadata_list=metadata_list,
                private_metadata_list=private_metadata_list,
            )
            # remove checkout after order is successfully created
            checkout_info.checkout.delete()
        except InsufficientStock as e:
            release_voucher_usage(
                order_data.get("voucher"), order_data.get("user_email")
            )
            gateway.payment_refund_or_void(
                payment, manager, channel_slug=checkout_info.channel.slug
            )
            error = prepare_insufficient_stock_checkout_validation_error(e)
            raise error
        except GiftCardNotApplicable as e:
            release_voucher_usage(
                order_data.get("voucher"), order_data.get("user_email")
            )
            gateway.payment_refund_or_void(
                payment, manager, channel_slug=checkout_info.channel.slug
            )
            raise ValidationError(code=e.code, message=e.message)

        # if the order total value is 0 it is paid from the definition
        if order.total.net.amount == 0:
            if (
                order.channel.order_mark_as_paid_strategy
                == MarkAsPaidStrategy.PAYMENT_FLOW
            ):
                mark_order_as_paid_with_payment(order, user, app, manager)

    return order, action_required, action_data


def _is_refund_ongoing(payment):
    """Return True if refund is ongoing for given payment."""
    return (
        payment.transactions.filter(
            kind=TransactionKind.REFUND_ONGOING, is_success=True
        ).exists()
        if payment
        else False
    )


def _increase_voucher_usage(checkout_info: "CheckoutInfo"):
    """Increase a voucher usage applied to the checkout."""
    voucher = get_voucher_for_checkout_info(checkout_info, with_lock=True)
    if not voucher:
        return None

    if voucher.apply_once_per_customer:
        customer_email = cast(str, checkout_info.get_customer_email())
        add_voucher_usage_by_customer(voucher, customer_email)

    if voucher.usage_limit:
        increase_voucher_usage(voucher)


def _create_order_lines_from_checkout_lines(
    checkout_info: CheckoutInfo,
    lines: List[CheckoutLineInfo],
    manager: "PluginsManager",
    order_pk: Union[str, UUID],
    prices_entered_with_tax: bool,
) -> List[OrderLineInfo]:
    order_lines_info = _create_lines_for_order(
        manager,
        checkout_info,
        lines,
        prices_entered_with_tax,
    )
    order_lines = []
    order_line_discounts: List["OrderLineDiscount"] = []
    for line_info in order_lines_info:
        line = line_info.line
        line.order_id = order_pk
        order_lines.append(line)
        if discounts := line_info.line_discounts:
            order_line_discounts.extend(discounts)

    OrderLine.objects.bulk_create(order_lines)
    OrderLineDiscount.objects.bulk_create(order_line_discounts)

    return list(order_lines_info)


def _handle_allocations_of_order_lines(
    checkout_info: CheckoutInfo,
    checkout_lines: List[CheckoutLineInfo],
    order_lines_info: List[OrderLineInfo],
    manager: "PluginsManager",
    reservation_enabled: bool,
):
    country_code = checkout_info.get_country()
    additional_warehouse_lookup = (
        checkout_info.delivery_method_info.get_warehouse_filter_lookup()
    )
    allocate_stocks(
        order_lines_info,
        country_code,
        checkout_info.channel,
        manager,
        checkout_info.delivery_method_info.warehouse_pk,
        additional_warehouse_lookup,
        check_reservations=True,
        checkout_lines=[line.line for line in checkout_lines],
    )
    allocate_preorders(
        order_lines_info,
        checkout_info.channel.slug,
        check_reservations=reservation_enabled,
        checkout_lines=[line.line for line in checkout_lines],
    )


def _handle_checkout_discount(order: "Order", checkout: "Checkout"):
    if checkout.discount:
        # store voucher as a fixed value as it this the simplest solution for now.
        # This will be solved when we refactor the voucher logic to use .discounts
        # relations

        order.discounts.create(
            type=DiscountType.VOUCHER,
            value_type=DiscountValueType.FIXED,
            value=checkout.discount.amount,
            name=checkout.discount_name,
            translated_name=checkout.translated_discount_name,
            currency=checkout.currency,
            amount_value=checkout.discount_amount,
        )


def _post_create_order_actions(
    order: "Order",
    checkout_info: "CheckoutInfo",
    order_lines_info: List["OrderLineInfo"],
    manager: "PluginsManager",
    user: Optional[User],
    app: Optional["App"],
    site_settings: "SiteSettings",
):
    order_info = OrderInfo(
        order=order,
        customer_email=order.user_email,
        channel=checkout_info.channel,
        payment=order.get_last_payment(),
        lines_data=order_lines_info,
    )

    transaction.on_commit(
        lambda: order_created(
            order_info=order_info,
            user=user,
            app=app,
            manager=manager,
            site_settings=site_settings,
        )
    )

    # Send the order confirmation email
    transaction.on_commit(
        lambda: send_order_confirmation(
            order_info, checkout_info.checkout.redirect_url, manager
        )
    )


def _create_order_from_checkout(
    checkout_info: CheckoutInfo,
    checkout_lines_info: List[CheckoutLineInfo],
    manager: "PluginsManager",
    user: Optional[User],
    app: Optional["App"],
    metadata_list: Optional[List] = None,
    private_metadata_list: Optional[List] = None,
):
    from ..order.utils import add_gift_cards_to_order

    site_settings = Site.objects.get_current().settings

    address = checkout_info.shipping_address or checkout_info.billing_address

    reservation_enabled = is_reservation_enabled(site_settings)
    tax_configuration = checkout_info.tax_configuration
    prices_entered_with_tax = tax_configuration.prices_entered_with_tax

    # total
    taxed_total = calculations.calculate_checkout_total_with_gift_cards(
        manager=manager,
        checkout_info=checkout_info,
        lines=checkout_lines_info,
        address=address,
    )

    # voucher
    voucher = checkout_info.voucher

    # shipping
    base_shipping_price = base_checkout_delivery_price(
        checkout_info, checkout_lines_info
    )
    shipping_total = calculations.checkout_shipping_price(
        manager=manager,
        checkout_info=checkout_info,
        lines=checkout_lines_info,
        address=address,
    )
    shipping_tax_rate = calculations.checkout_shipping_tax_rate(
        manager=manager,
        checkout_info=checkout_info,
        lines=checkout_lines_info,
        address=address,
    )

    # status
    status = (
        OrderStatus.UNFULFILLED
        if (
            checkout_info.channel.automatically_confirm_all_new_orders
            and checkout_info.checkout.payment_transactions.exists()
        )
        else OrderStatus.UNCONFIRMED
    )
    checkout_metadata = get_or_create_checkout_metadata(checkout_info.checkout)

    # update metadata
    if metadata_list:
        checkout_metadata.store_value_in_metadata(
            {data.key: data.value for data in metadata_list}
        )
    if private_metadata_list:
        checkout_metadata.store_value_in_private_metadata(
            {data.key: data.value for data in private_metadata_list}
        )

    # order
    order = Order.objects.create(  # type: ignore[misc] # see below:
        status=status,
        language_code=checkout_info.checkout.language_code,
        total=taxed_total,  # money field not supported by mypy_django_plugin
        shipping_tax_rate=shipping_tax_rate,
        voucher=voucher,
        checkout_token=str(checkout_info.checkout.token),
        origin=OrderOrigin.CHECKOUT,
        channel=checkout_info.channel,
        metadata=checkout_metadata.metadata,
        private_metadata=checkout_metadata.private_metadata,
        redirect_url=checkout_info.checkout.redirect_url,
        should_refresh_prices=False,
        tax_exemption=checkout_info.checkout.tax_exemption,
        **_process_shipping_data_for_order(
            checkout_info,
            base_shipping_price,
            shipping_total,
            manager,
            checkout_lines_info,
        ),
        **_process_user_data_for_order(checkout_info, manager),
    )

    # checkout discount
    _handle_checkout_discount(order, checkout_info.checkout)

    # lines
    order_lines_info = _create_order_lines_from_checkout_lines(
        checkout_info=checkout_info,
        lines=checkout_lines_info,
        manager=manager,
        order_pk=order.pk,
        prices_entered_with_tax=prices_entered_with_tax,
    )

    # update undiscounted order total
    undiscounted_total = (
        sum(
            [line.line.undiscounted_total_price for line in order_lines_info],
            start=zero_taxed_money(taxed_total.currency),
        )
        + shipping_total
    )
    order.undiscounted_total = undiscounted_total
    order.save(
        update_fields=[
            "undiscounted_total_net_amount",
            "undiscounted_total_gross_amount",
        ]
    )

    # allocations
    _handle_allocations_of_order_lines(
        checkout_info=checkout_info,
        checkout_lines=checkout_lines_info,
        order_lines_info=order_lines_info,
        manager=manager,
        reservation_enabled=reservation_enabled,
    )

    # giftcards
    currency = checkout_info.checkout.currency
    subtotal_list = [line.line.total_price for line in order_lines_info]
    subtotal = sum(subtotal_list, zero_taxed_money(currency))
    total_without_giftcard = subtotal + shipping_total - checkout_info.checkout.discount
    add_gift_cards_to_order(
        checkout_info, order, total_without_giftcard.gross, user, app
    )

    # payments
    checkout_info.checkout.payments.update(order=order, checkout_id=None)
    checkout_info.checkout.payment_transactions.update(order=order, checkout_id=None)
    update_order_charge_data(order, with_save=False)
    update_order_authorize_data(order, with_save=False)

    # tax settings
    update_order_display_gross_prices(order)

    # order search
    order.search_vector = FlatConcatSearchVector(
        *prepare_order_search_vector_value(order)
    )
    order.save()

    # post create actions
    _post_create_order_actions(
        order=order,
        checkout_info=checkout_info,
        order_lines_info=order_lines_info,
        manager=manager,
        user=user,
        app=app,
        site_settings=site_settings,
    )
    return order


def create_order_from_checkout(
    checkout_info: CheckoutInfo,
    manager: "PluginsManager",
    user: Optional["User"],
    app: Optional["App"],
    delete_checkout: bool = True,
    metadata_list: Optional[List] = None,
    private_metadata_list: Optional[List] = None,
) -> Order:
    """Crate order from checkout.

    If checkout doesn't have all required data, the function will raise ValidationError.

    Each order will get a private copy of both the billing and the shipping
    address (if shipping).

    If any of the addresses is new and the user is logged in the address
    will also get saved to that user's address book.

    Current user's language is saved in the order so we can later determine
    which language to use when sending email.

    Checkout can be deleted by setting flag `delete_checkout` to True

    :raises: InsufficientStock, GiftCardNotApplicable
    """

    voucher = None
    if voucher := checkout_info.voucher:
        with transaction.atomic():
            _increase_voucher_usage(checkout_info=checkout_info)

    with transaction.atomic():
        checkout_pk = checkout_info.checkout.pk
        checkout = Checkout.objects.select_for_update().filter(pk=checkout_pk).first()
        if not checkout:
            order = Order.objects.get_by_checkout_token(checkout_pk)
            return order

        # Fetching checkout info inside the transaction block with select_for_update
        # ensure that we are processing checkout on the current data.
        checkout_lines, _ = fetch_checkout_lines(checkout, voucher=voucher)
        checkout_info = fetch_checkout_info(
            checkout, checkout_lines, manager, voucher=voucher
        )
        assign_checkout_user(user, checkout_info)

        try:
            order = _create_order_from_checkout(
                checkout_info=checkout_info,
                checkout_lines_info=list(checkout_lines),
                manager=manager,
                user=user,
                app=app,
                metadata_list=metadata_list,
                private_metadata_list=private_metadata_list,
            )
            if delete_checkout:
                checkout_info.checkout.delete()
            return order
        except InsufficientStock:
            release_voucher_usage(
                checkout_info.voucher, checkout_info.checkout.get_customer_email()
            )
            raise
        except GiftCardNotApplicable:
            release_voucher_usage(
                checkout_info.voucher, checkout_info.checkout.get_customer_email()
            )
            raise


def assign_checkout_user(
    user: Optional["User"],
    checkout_info: "CheckoutInfo",
):
    # Assign checkout user to an existing user if checkout email matches a valid
    #  customer account
    if user is None and not checkout_info.user and checkout_info.checkout.email:
        existing_user = retrieve_user_by_email(checkout_info.checkout.email)
        checkout_info.user = (
            existing_user if existing_user and existing_user.is_active else None
        )


def complete_checkout(
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    payment_data: Dict[Any, Any],
    store_source: bool,
    user: Optional["User"],
    app: Optional["App"],
    site_settings: Optional["SiteSettings"] = None,
    redirect_url: Optional[str] = None,
    metadata_list: Optional[List] = None,
    private_metadata_list: Optional[List] = None,
) -> Tuple[Optional[Order], bool, dict]:
    transactions = checkout_info.checkout.payment_transactions.all()
    fetch_checkout_data(checkout_info, manager, lines)

    # When checkout is zero, we don't need any transaction to cover the checkout total.
    # We check if checkout is zero, and we also check what flow for marking an order as
    # paid is used. In case when we have TRANSACTION_FLOW we use transaction flow to
    # finalize the checkout.
    checkout_is_zero = checkout_info.checkout.total.gross.amount == Decimal(0)
    is_transaction_flow = (
        checkout_info.channel.order_mark_as_paid_strategy
        == MarkAsPaidStrategy.TRANSACTION_FLOW
    )
    if (
        transactions
        or checkout_info.channel.allow_unpaid_orders
        or checkout_is_zero
        and is_transaction_flow
    ):
        order = complete_checkout_with_transaction(
            manager=manager,
            checkout_info=checkout_info,
            lines=lines,
            user=user,
            app=app,
            redirect_url=redirect_url,
            metadata_list=metadata_list,
            private_metadata_list=private_metadata_list,
        )
        return order, False, {}

    return complete_checkout_with_payment(
        manager=manager,
        checkout_pk=checkout_info.checkout.pk,
        payment_data=payment_data,
        store_source=store_source,
        user=user,
        app=app,
        site_settings=site_settings,
        redirect_url=redirect_url,
        metadata_list=metadata_list,
        private_metadata_list=private_metadata_list,
    )


def complete_checkout_with_transaction(
    manager: "PluginsManager",
    checkout_info: "CheckoutInfo",
    lines: Iterable["CheckoutLineInfo"],
    user: Optional["User"],
    app: Optional["App"],
    redirect_url: Optional[str] = None,
    metadata_list: Optional[List] = None,
    private_metadata_list: Optional[List] = None,
) -> Optional[Order]:
    try:
        _prepare_checkout_with_transactions(
            manager=manager,
            checkout_info=checkout_info,
            lines=lines,
            redirect_url=redirect_url,
        )

        return create_order_from_checkout(
            checkout_info=checkout_info,
            manager=manager,
            user=user,
            app=app,
            delete_checkout=True,
            metadata_list=metadata_list,
            private_metadata_list=private_metadata_list,
        )
    except NotApplicable:
        raise ValidationError(
            {
                "voucher_code": ValidationError(
                    "Voucher not applicable",
                    code=CheckoutErrorCode.VOUCHER_NOT_APPLICABLE.value,
                )
            }
        )
    except InsufficientStock as e:
        error = prepare_insufficient_stock_checkout_validation_error(e)
        raise error
    except GiftCardNotApplicable as e:
        raise ValidationError({"gift_cards": e})


def complete_checkout_with_payment(
    manager: "PluginsManager",
    checkout_pk: UUID,
    payment_data,
    store_source,
    user,
    app,
    site_settings=None,
    redirect_url=None,
    metadata_list: Optional[List] = None,
    private_metadata_list: Optional[List] = None,
) -> Tuple[Optional[Order], bool, dict]:
    """Logic required to finalize the checkout and convert it to order.

    Should be used with transaction_with_commit_on_errors, as there is a possibility
    for thread race.
    :raises ValidationError
    """
    with transaction_with_commit_on_errors():
        checkout = Checkout.objects.select_for_update().filter(pk=checkout_pk).first()
        if not checkout:
            order = Order.objects.get_by_checkout_token(checkout_pk)
            return order, False, {}

        # Fetching checkout info inside the transaction block with select_for_update
        # enure that we are processing checkout on the current data.
        lines, _ = fetch_checkout_lines(checkout)
        checkout_info = fetch_checkout_info(checkout, lines, manager)
        assign_checkout_user(user, checkout_info)

        payment, customer_id, order_data = complete_checkout_pre_payment_part(
            manager=manager,
            checkout_info=checkout_info,
            lines=lines,
            user=user,
            site_settings=site_settings,
            redirect_url=redirect_url,
        )

        _reserve_stocks_without_availability_check(checkout_info, lines)

    # Process payments out of transaction to unlock stock rows for another user,
    # who potentially can order the same product variants.
    txn = None
    channel_slug = checkout_info.channel.slug
    if payment:
        txn = _process_payment(
            payment=payment,
            customer_id=customer_id,
            store_source=store_source,
            payment_data=payment_data,
            order_data=order_data,
            manager=manager,
            channel_slug=channel_slug,
        )

        # As payment processing might take a while, we need to check if the payment
        # doesn't become inactive in the meantime. If it's inactive we need to refund
        # the payment.
        payment.refresh_from_db()
        if not payment.is_active:
            gateway.payment_refund_or_void(payment, manager, channel_slug=channel_slug)
            raise ValidationError(
                f"The payment with pspReference: {payment.psp_reference} is inactive.",
                code=CheckoutErrorCode.INACTIVE_PAYMENT.value,
            )

    with transaction_with_commit_on_errors():
        checkout = (
            Checkout.objects.select_for_update()
            .filter(pk=checkout_info.checkout.pk)
            .first()
        )
        if not checkout:
            order = Order.objects.get_by_checkout_token(checkout_info.checkout.token)
            return order, False, {}

        # We need to refetch the checkout info to ensure that we process checkout
        # for correct data.
        lines, _ = fetch_checkout_lines(checkout, skip_recalculation=True)
        checkout_info = fetch_checkout_info(checkout, lines, manager)

        checkout_info.checkout.voucher_code = None
        order, action_required, action_data = complete_checkout_post_payment_part(
            manager=manager,
            checkout_info=checkout_info,
            lines=lines,
            payment=payment,
            txn=txn,
            order_data=order_data,
            user=user,
            app=app,
            site_settings=site_settings,
            metadata_list=metadata_list,
            private_metadata_list=private_metadata_list,
        )

    return order, action_required, action_data


def _reserve_stocks_without_availability_check(
    checkout_info: CheckoutInfo,
    lines: Iterable[CheckoutLineInfo],
):
    """Add additional temporary reservation for stock.

    Due to unlocking rows, for the time of external payment call, it prevents users
    ordering the same product, in the same time, which is out of stock.
    """
    variants = [line.variant for line in lines]
    stocks = Stock.objects.get_variants_stocks_for_country(
        country_code=checkout_info.get_country(),
        channel_slug=checkout_info.channel.slug,
        products_variants=variants,
    )
    variants_stocks_map = {stock.product_variant_id: stock for stock in stocks}

    reservations = []
    for line in lines:
        if line.variant.id in variants_stocks_map:
            reservations.append(
                Reservation(
                    quantity_reserved=line.line.quantity,
                    reserved_until=timezone.now()
                    + timedelta(seconds=settings.RESERVE_DURATION),
                    stock=variants_stocks_map[line.variant.id],
                    checkout_line=line.line,
                )
            )
    Reservation.objects.bulk_create(reservations)
    return reservations

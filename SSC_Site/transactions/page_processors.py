import uuid
import string
import hashlib
import datetime
from random import choice
from itertools import chain
import requests as web_request
from django.utils import timezone
from mezzanine.forms import fields
from mezzanine.conf import settings
from django.shortcuts import render
from django.core.urlresolvers import reverse
from mezzanine.forms.models import FieldEntry
from django.utils.translation import ugettext as _
from mezzanine.pages.page_processors import processor_for
from mezzanine.forms.page_processors import form_processor
from mezzanine.utils.email import split_addresses, send_mail_template
from .models import PaymentFormPage, PriceGroup, UpalPaymentTransaction


@processor_for(PaymentFormPage)
def payment_form_processor(request, page):
    payment_form = page.paymentformpage.payment_form
    payment_form.form.send_email = False

    upal_transactions = get_upal_transactions_info(page.paymentformpage)

    successful_payments = 0
    if page.paymentformpage.payment_gateway.type == "upal":
        successful_payments += upal_transactions.filter(is_payed=True).count()

    if payment_form.form.fields.filter(label="UUID").count() != 1:
        return {"status": "design_error"}

    uuid_key = "field_{}".format(payment_form.form.fields.get(label="UUID").id)
    if request.POST:
        mutable = request.POST._mutable
        request.POST._mutable = True
        request_uuid = uuid.uuid4()
        request.POST[uuid_key] = request_uuid
        request.POST._mutable = mutable

    form = form_processor(request, payment_form)

    if isinstance(form, dict) and form.has_key("form"):
        if request.user.has_perm('transactions.can_view_payment_transactions'):
            if page.paymentformpage.payment_gateway.type == "upal":
                form_fields = page.paymentformpage.payment_form.form.fields.all().order_by("id")
                form_fields = [field.label for field in form_fields]
                for title in [_("Creation Time"), _("Form Entry UUID"), _("Bank Token"), _("Random Token"),
                              _("Price Group"), _("Amount in Rials"), _("Is Payed"), _("Payment Time")]:
                    form_fields.append(title)
                upal_transactions.filter(creation_time__lt=timezone.now() - datetime.timedelta(minutes=20),
                                         is_payed=None).update(is_payed=False)
                successful_transactions = upal_transactions.filter(is_payed=True)
                pending_transactions = upal_transactions.filter(is_payed=None)
                failed_transactions = upal_transactions.filter(is_payed=False)
                transactions = chain(successful_transactions, pending_transactions, failed_transactions)
                transactions_info = []
                for transaction in transactions:
                    entries = get_transaction_entries(transaction)
                    if entries is not None:
                        entries = [entry.value for entry in entries]
                        for value in [transaction.creation_time, transaction.uuid , transaction.bank_token,
                                      transaction.random_token,
                                      transaction.price_group.group_identifier + " (" +str(transaction.price_group.payment_amount) + ")",
                                      transaction.payment_amount,
                                      transaction.is_payed, transaction.payment_time]:
                            entries.append(value)
                        transactions_info.append(entries)

            return {"status": "form", "form": form["form"],
                    "form_fields": form_fields, "transactions_info": transactions_info}

        if page.paymentformpage.capacity != 0:
            if successful_payments >= page.paymentformpage.capacity:
                return {"status": "at_full_capacity"}

        return {"status": "form", "form": form["form"]}

    plan = PriceGroup.objects.get(id=request.POST.get("payment_plan_id"))
    if plan.capacity != 0:
        plan_successful_payments = 0
        if page.paymentformpage.payment_gateway.type == "upal":
            plan_successful_payments += upal_transactions.filter(is_payed=True, price_group=plan).count()

        if plan_successful_payments >= plan.capacity:
            return {"status": "at_full_capacity"}

    transaction = new_payment(request, page.paymentformpage, plan, request_uuid)

    if transaction is None:
        return {"status": "gateway_error"}

    payment_url = 'http://upal.ir/transaction/submit?id={}'.format(transaction.bank_token)

    if page.paymentformpage.capacity != 0:
        if page.paymentformpage.payment_gateway.type == "upal":
            pending_payments = successful_payments + upal_transactions.filter(
                is_payed=None, creation_time__gt=timezone.now() - datetime.timedelta(minutes=10)).count()
        if pending_payments > page.paymentformpage.capacity:
            return {"status": "payment", "payment_url": payment_url, "warning": "reserved_list"}

    if plan.capacity != 0:
        if page.paymentformpage.payment_gateway.type == "upal":
            plan_pending_payments = plan_successful_payments + upal_transactions.filter(
                    is_payed=None, creation_time__gt=timezone.now() - datetime.timedelta(minutes=10),
                    price_group=plan).count()
        if plan_pending_payments > plan.capacity:
            return {"status": "payment", "payment_url": payment_url, "warning": "reserved_list"}

    return {"status": "payment", "payment_url": payment_url}


def new_payment(request, paymentformpage, plan, request_uuid):
    if paymentformpage.payment_gateway.type == "upal":
        random_token = ''.join(choice(string.lowercase + string.uppercase + string.digits) for i in range(16))
        transaction = UpalPaymentTransaction(creation_time=timezone.now(),
                                             uuid= request_uuid,
                                             random_token=random_token,
                                             price_group=plan,
                                             payment_amount=plan.payment_amount)
        transaction.save()
        return_url = request.build_absolute_uri(reverse('transactions_from_bank', args=('upal', transaction.id)))
        try:
            payment_request = web_request.post("http://upal.ir//transaction/create",
                                               data={'gateway_id': paymentformpage.payment_gateway.gateway_id,
                                                     'amount': plan.payment_amount,
                                                     'description': u"{}-{}".format(paymentformpage.payment_description, plan.group_identifier),
                                                     'rand': random_token,
                                                     'redirect_url': return_url,
                                                     })
        except web_request.ConnectionError:
            transaction.delete()
            return None

        if not (payment_request.status_code == 200 and payment_request.reason == 'OK'):
            return None
        transaction.bank_token = payment_request.text
        transaction.save()

        return transaction


def get_transaction_entries(transaction):
    if FieldEntry.objects.filter(value=transaction.uuid).count() == 1:
        entry = FieldEntry.objects.get(value=transaction.uuid).entry
        field_entries = FieldEntry.objects.filter(entry=entry).order_by("field_id")
        return field_entries
    return None


def from_bank(request, transaction_type, transaction_id):
    if transaction_type == 'upal':
        bank_token = request.GET.get('trans_id')
        validation_hash = request.GET.get('valid')
        transaction = UpalPaymentTransaction.objects.get(id=transaction_id)
        if bank_token == transaction.bank_token:
            our_validation_md5 = hashlib.md5()
            our_validation_md5.update("{}{}{}{}".format(transaction.price_group.payment_form_page.payment_gateway.gateway_id,
                                                        transaction.payment_amount,
                                                        transaction.price_group.payment_form_page.payment_gateway.gateway_api,
                                                        transaction.random_token))
            if our_validation_md5.hexdigest() == validation_hash:

                send_payment_main = False
                if transaction.is_payed is None or transaction.is_payed is False:
                    transaction.is_payed = True
                    transaction.payment_time = timezone.now()
                    transaction.save()

                    send_payment_main = True


                form = transaction.price_group.payment_form_page.payment_form.form
                form_fields = form.fields.all().order_by("id")

                field_entries = get_transaction_entries(transaction)

                email_from = form.email_from or settings.DEFAULT_FROM_EMAIL

                field_tuples = []
                email_to = None
                for field, field_entry in zip(form_fields, field_entries):
                    field_tuples.append((field.label, field_entry.value))
                    if field.is_a(fields.EMAIL):
                        email_to = field_entry.value

                field_tuples.append((_('Price Group'), transaction.price_group.group_identifier))
                field_tuples.append((_('Payment in Rials'), transaction.payment_amount))

                subject = form.email_subject

                context = {
                    "fields": field_tuples,
                    "message": form.email_message,
                    "request": request,
                }

                if send_payment_main:
                    send_mail_template(subject, "email/form_response", email_from, email_to, context)

                    email_copies = split_addresses(form.email_copies)
                    if email_copies:
                        send_mail_template(subject, "email/form_response_copies",
                                           email_from, email_copies, context)

                return render(request, 'pages/message.html', {"page": transaction.price_group.payment_form_page,
                                                              "title": _("Successful Payment Transaction"),
                                                              "context": context})
            else:
                # print(our_validation_md5.hexdigest())
                # print(validation_hash)
                transaction.is_payed = False
                transaction.save()

    return render(request, 'pages/error.html', {"page": transaction.price_group.payment_form_page,
                                                "title": _("UnSuccessful Payment Transaction")})


def get_upal_transactions_info(paymentformpage):
    transactions = UpalPaymentTransaction.objects.filter(price_group__payment_form_page=paymentformpage)
    return transactions
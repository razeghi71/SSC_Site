from copy import deepcopy
from django.contrib import admin
from mezzanine.pages.admin import PageAdmin
from .models import PaymentFormPage, PriceGroup, PaymentGateway
from mezzanine.core import admin as mezzanineAdmin


class PaymentGatewayAdmin(mezzanineAdmin.BaseTranslationModelAdmin):
    fieldsets = ((None, {"fields": ("title", "type", "gateway_id", "gateway_api")}),)
    list_display = ("type", "title", "gateway_id", "gateway_api")
    list_display_links = ("title", "gateway_id", "gateway_api")
    list_filter = ("type", "title", "gateway_id", "gateway_api")
    search_fields = ("type", "title")

admin.site.register(PaymentGateway, PaymentGatewayAdmin)

form_fieldsets = deepcopy(PageAdmin.fieldsets)
form_fieldsets[0][1]["fields"].insert(+2, "payment_form")
form_fieldsets[0][1]["fields"].insert(+3, "payment_gateway")
form_fieldsets[0][1]["fields"].insert(+4, "payment_description")
form_fieldsets[0][1]["fields"].insert(+5, "capacity")
form_fieldsets[0][1]["fields"].insert(+6, "content")


class PriceGroupInline(mezzanineAdmin.TabularDynamicInlineAdmin):
    model = PriceGroup


class PaymentFormPageAdmin(PageAdmin):
    inlines = (PriceGroupInline,)
    fieldsets = form_fieldsets


admin.site.register(PaymentFormPage, PaymentFormPageAdmin)

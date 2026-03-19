"""Validator signal CRUD: create, update, delete, and list operations."""

import logging

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from django.views.generic.edit import FormView

from validibot.core.utils import reverse_with_org
from validibot.validations.constants import SignalDirection
from validibot.validations.forms import SignalDefinitionForm
from validibot.validations.models import SignalDefinition
from validibot.validations.models import Validator
from validibot.validations.views.validators import CustomValidatorManageMixin

logger = logging.getLogger(__name__)


class ValidatorSignalMixin(CustomValidatorManageMixin):
    """Common helpers for validator signal CRUD."""

    validator: Validator

    def dispatch(self, request, *args, **kwargs):
        self.validator = get_object_or_404(
            Validator,
            pk=self.kwargs.get("pk"),
            is_system=False,
        )
        return super().dispatch(request, *args, **kwargs)

    def _hx_redirect(self):
        url = reverse_with_org(
            "validations:validator_detail",
            request=self.request,
            kwargs={"slug": self.validator.slug},
        )
        from django.http import HttpResponse

        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response

    def _redirect(self):
        return redirect(
            reverse_with_org(
                "validations:validator_detail",
                request=self.request,
                kwargs={"slug": self.validator.slug},
            ),
        )


class ValidatorSignalCreateView(ValidatorSignalMixin, FormView):
    form_class = SignalDefinitionForm

    def get(self, request, *args, **kwargs):
        """Handle GET requests to return fresh form content for HTMx modal."""
        direction = request.GET.get("direction") or SignalDirection.INPUT
        form = self.form_class(
            initial={"direction": direction}, validator=self.validator
        )
        if not self.validator.has_processor:
            form.fields["direction"].widget = forms.HiddenInput()

        if request.headers.get("HX-Request"):
            return render(
                request,
                "validations/library/partials/modal_signal_create.html",
                {
                    "validator": self.validator,
                    "modal_form": form,
                    "modal_id": "modal-signal-create",
                    "modal_title": _("Add Signal"),
                },
            )
        # Non-HTMx GET request - redirect to validator detail
        return self._redirect()

    def post(self, request, *args, **kwargs):
        direction = request.POST.get("direction") or SignalDirection.INPUT
        form = self.form_class(
            request.POST,
            initial={"direction": direction},
            validator=self.validator,
        )
        if not self.validator.has_processor:
            form.fields["direction"].widget = forms.HiddenInput()
        if form.is_valid():
            signal = form.save(commit=False)
            signal.validator = self.validator
            signal.save()
            messages.success(request, _("Signal created."))
            if request.headers.get("HX-Request"):
                return self._hx_redirect()
            return self._redirect()
        if request.headers.get("HX-Request"):
            return render(
                request,
                "validations/library/partials/modal_signal_create.html",
                {
                    "validator": self.validator,
                    "modal_form": form,
                    "modal_id": "modal-signal-create",
                    "modal_title": _("Add Signal"),
                },
                status=200,
            )
        messages.error(request, _("Please correct the errors below."))
        return self._redirect()


class ValidatorSignalUpdateView(ValidatorSignalMixin, FormView):
    form_class = SignalDefinitionForm

    def post(self, request, *args, **kwargs):
        signal = get_object_or_404(
            SignalDefinition,
            pk=self.kwargs.get("entry_pk"),
            validator=self.validator,
        )
        form = self.form_class(request.POST, instance=signal, validator=self.validator)
        if form.is_valid():
            form.save()
            messages.success(request, _("Signal updated."))
            if request.headers.get("HX-Request"):
                return self._hx_redirect()
            return self._redirect()
        if request.headers.get("HX-Request"):
            return render(
                request,
                "validations/library/partials/modal_signal_edit.html",
                {
                    "validator": self.validator,
                    "entry_id": signal.id,
                    "form": form,
                },
                status=200,
            )
        messages.error(request, _("Please correct the errors below."))
        return self._redirect()


class ValidatorSignalDeleteView(ValidatorSignalMixin, TemplateView):
    def post(self, request, *args, **kwargs):
        signal = get_object_or_404(
            SignalDefinition,
            pk=self.kwargs.get("entry_pk"),
            validator=self.validator,
        )
        try:
            signal.delete()
            messages.success(request, _("Signal deleted."))
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        if request.headers.get("HX-Request"):
            return self._hx_redirect()
        return self._redirect()


class ValidatorSignalListView(ValidatorSignalMixin, TemplateView):
    """Legacy list route redirects to the validator detail page."""

    def get(self, request, *args, **kwargs):
        return redirect(
            reverse_with_org(
                "validations:validator_detail",
                request=request,
                kwargs={"pk": self.validator.pk},
            ),
        )

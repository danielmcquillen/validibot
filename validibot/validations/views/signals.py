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
from validibot.validations.constants import CatalogRunStage
from validibot.validations.forms import ValidatorCatalogEntryForm
from validibot.validations.models import Validator
from validibot.validations.models import ValidatorCatalogEntry
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
    form_class = ValidatorCatalogEntryForm

    def get(self, request, *args, **kwargs):
        """Handle GET requests to return fresh form content for HTMx modal."""
        stage = request.GET.get("run_stage") or CatalogRunStage.INPUT
        form = self.form_class(initial={"run_stage": stage}, validator=self.validator)
        if not self.validator.has_processor:
            form.fields["run_stage"].widget = forms.HiddenInput()

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
        stage = request.POST.get("run_stage") or CatalogRunStage.INPUT
        form = self.form_class(
            request.POST,
            initial={"run_stage": stage},
            validator=self.validator,
        )
        if not self.validator.has_processor:
            form.fields["run_stage"].widget = forms.HiddenInput()
        if form.is_valid():
            entry = form.save(commit=False)
            entry.validator = self.validator
            entry.save()
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
    form_class = ValidatorCatalogEntryForm

    def post(self, request, *args, **kwargs):
        entry = get_object_or_404(
            ValidatorCatalogEntry,
            pk=self.kwargs.get("entry_pk"),
            validator=self.validator,
        )
        form = self.form_class(request.POST, instance=entry, validator=self.validator)
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
                    "entry_id": entry.id,
                    "form": form,
                },
                status=200,
            )
        messages.error(request, _("Please correct the errors below."))
        return self._redirect()


class ValidatorSignalDeleteView(ValidatorSignalMixin, TemplateView):
    def post(self, request, *args, **kwargs):
        entry = get_object_or_404(
            ValidatorCatalogEntry,
            pk=self.kwargs.get("entry_pk"),
            validator=self.validator,
        )
        try:
            entry.delete()
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

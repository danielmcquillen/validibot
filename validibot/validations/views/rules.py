"""Validator rule CRUD: create, update, move, delete, and list operations."""

import logging
import re

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from django.views.generic import View
from django.views.generic.edit import FormView

from validibot.core.utils import reverse_with_org
from validibot.users.permissions import PermissionCode
from validibot.validations.constants import AssertionType
from validibot.validations.constants import Severity
from validibot.validations.forms import ValidatorRuleForm
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import SignalDefinition
from validibot.validations.models import Validator
from validibot.validations.views.validators import CustomValidatorManageMixin

logger = logging.getLogger(__name__)


class ValidatorRuleMixin(CustomValidatorManageMixin):
    """Common helpers for validator default assertion CRUD."""

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
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response

    def _can_move_rule(self) -> bool:
        membership = self.get_active_membership()
        if not membership:
            return False
        if self.request.user.has_perm(
            PermissionCode.ADMIN_MANAGE_ORG.value,
            self.validator,
        ):
            return True
        if not self.request.user.has_perm(
            PermissionCode.WORKFLOW_EDIT.value,
            self.validator,
        ):
            return False
        custom = getattr(self.validator, "custom_validator", None)
        return bool(custom and custom.created_by_id == self.request.user.id)

    def _redirect(self):
        return redirect(
            reverse_with_org(
                "validations:validator_detail",
                request=self.request,
                kwargs={"slug": self.validator.slug},
            ),
        )

    def _resolve_selected_entries(self, signals: list[str]) -> list[SignalDefinition]:
        ids = [int(pk) for pk in signals or [] if str(pk).isdigit()]
        return list(
            self.validator.signal_definitions.filter(pk__in=ids).order_by(
                "contract_key",
            ),
        )

    def _validate_cel_expression(
        self, expr: str, available_entries: list[SignalDefinition]
    ) -> list[SignalDefinition]:
        """Validate CEL and return the signal definitions that are referenced.

        The parser enforces the namespaced convention: all data references
        must use a namespace prefix (``p.``, ``s.``, ``output.``, ``steps.``).
        Bare identifiers are rejected.

        Output signals may be referenced as ``output.<contract_key>`` or
        ``o.<contract_key>``.
        """
        expr = (expr or "").strip()
        if not expr:
            raise ValidationError(_("CEL expression is required."))
        if not self._delimiters_balanced(expr):
            raise ValidationError(_("Parentheses and brackets must be balanced."))

        reserved_literals = {"true", "false", "null"}
        namespace_roots = {"p", "payload", "s", "signal", "o", "output", "steps"}
        cel_builtins = {
            "has",
            "exists",
            "exists_one",
            "all",
            "map",
            "filter",
            "size",
            "contains",
            "startsWith",
            "endsWith",
            "type",
            "int",
            "double",
            "string",
            "bool",
            "abs",
            "ceil",
            "floor",
            "round",
            "timestamp",
            "duration",
            "matches",
            "in",
            "is_int",
            "percentile",
            "mean",
            "sum",
            "max",
            "min",
        }

        key_map = {sig.contract_key: sig for sig in available_entries}
        referenced: set[SignalDefinition] = set()
        unknown: set[str] = set()

        # Strip string literals (including escaped quotes) so identifiers
        # inside quotes are not treated as bare identifiers.
        stripped = re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', "", expr)
        # Match namespaced references and track which signals are used.
        for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_\.]*", stripped):
            name = match.group(0)
            if name in reserved_literals or name in cel_builtins:
                continue
            if len(name) == 1:
                continue
            root = name.split(".")[0]
            if root in namespace_roots:
                # Track referenced signal definitions for output.*
                if (root in ("output", "o") and "." in name) or (
                    root == "s" and "." in name
                ):
                    key = name.split(".", 1)[1]
                    if key in key_map:
                        referenced.add(key_map[key])
                continue
            unknown.add(name)

        if unknown:
            raise ValidationError(
                _(
                    "Bare identifiers are not allowed. Use p.%(first)s "
                    "for payload data or s.%(first)s for workflow signals. "
                    "Unknown: %(names)s"
                )
                % {
                    "first": sorted(unknown)[0],
                    "names": ", ".join(sorted(unknown)),
                }
            )
        return list(referenced)

    @staticmethod
    def _delimiters_balanced(expression: str) -> bool:
        pairs = {"(": ")", "[": "]", "{": "}"}
        stack: list[str] = []
        for char in expression:
            if char in pairs:
                stack.append(pairs[char])
            elif char in pairs.values():
                if not stack or stack.pop() != char:
                    return False
        return not stack


class ValidatorRuleCreateView(ValidatorRuleMixin, FormView):
    form_class = ValidatorRuleForm

    def post(self, request, *args, **kwargs):
        form = self.form_class(
            request.POST,
            signal_choices=[
                (sig.id, sig.contract_key)
                for sig in self.validator.signal_definitions.order_by("contract_key")
            ],
        )
        if form.is_valid():
            available_signals = list(
                self.validator.signal_definitions.order_by("contract_key"),
            )
            cel_expr = form.cleaned_data["cel_expression"]
            referenced_signals = self._validate_cel_expression(
                cel_expr,
                available_signals,
            )
            # Pick the first referenced signal as the assertion target.
            # CEL assertions don't strictly need one, but it's useful for
            # display and deletion-protection.
            target_signal = referenced_signals[0] if referenced_signals else None
            default_ruleset = self.validator.ensure_default_ruleset()
            RulesetAssertion.objects.create(
                ruleset=default_ruleset,
                assertion_type=AssertionType.CEL_EXPRESSION,
                operator="",
                target_signal_definition=target_signal,
                target_data_path="" if target_signal else cel_expr,
                rhs={"expr": cel_expr},
                severity=Severity.ERROR,
                order=form.cleaned_data.get("order") or 0,
                message_template=form.cleaned_data["name"],
                cel_cache=cel_expr,
            )
            messages.success(request, _("Default assertion created."))
            if request.headers.get("HX-Request"):
                return self._hx_redirect()
            return self._redirect()
        if request.headers.get("HX-Request"):
            return render(
                request,
                "validations/library/partials/modal_rule_create.html",
                {
                    "validator": self.validator,
                    "default_assertion_create_form": form,
                },
                status=400,
            )
        messages.error(request, _("Please correct the errors below."))
        return self._redirect()


class ValidatorRuleUpdateView(ValidatorRuleMixin, FormView):
    form_class = ValidatorRuleForm

    def post(self, request, *args, **kwargs):
        default_ruleset = self.validator.default_ruleset
        assertion = get_object_or_404(
            RulesetAssertion,
            pk=self.kwargs.get("rule_pk"),
            ruleset=default_ruleset,
        )
        form = self.form_class(
            request.POST,
            signal_choices=[
                (sig.id, sig.contract_key)
                for sig in self.validator.signal_definitions.order_by("contract_key")
            ],
        )
        if form.is_valid():
            available_signals = list(
                self.validator.signal_definitions.order_by("contract_key"),
            )
            cel_expr = form.cleaned_data["cel_expression"]
            referenced_signals = self._validate_cel_expression(
                cel_expr,
                available_signals,
            )
            target_signal = referenced_signals[0] if referenced_signals else None
            assertion.message_template = form.cleaned_data["name"]
            assertion.rhs = {"expr": cel_expr}
            assertion.cel_cache = cel_expr
            assertion.order = form.cleaned_data.get("order") or 0
            assertion.target_signal_definition = target_signal
            assertion.target_data_path = "" if target_signal else cel_expr
            assertion.save()
            messages.success(request, _("Default assertion updated."))
            if request.headers.get("HX-Request"):
                return self._hx_redirect()
            return self._redirect()
        if request.headers.get("HX-Request"):
            return render(
                request,
                "validations/library/partials/modal_rule_edit.html",
                {
                    "validator": self.validator,
                    "rule_id": assertion.id,
                    "form": form,
                },
                status=400,
            )
        messages.error(request, _("Please correct the errors below."))
        return self._redirect()


class ValidatorRuleMoveView(ValidatorRuleMixin, View):
    """Move a default assertion up or down within a validator."""

    def post(self, request, *args, **kwargs):
        if not self._can_move_rule():
            return HttpResponse(status=403)
        default_ruleset = self.validator.default_ruleset
        if not default_ruleset:
            return HttpResponse(status=404)
        direction = request.POST.get("direction")
        assertion = get_object_or_404(
            RulesetAssertion,
            pk=self.kwargs.get("rule_pk"),
            ruleset=default_ruleset,
        )
        items = list(
            default_ruleset.assertions.order_by("order", "pk"),
        )
        try:
            index = items.index(assertion)
        except ValueError:
            return HttpResponse(status=404)

        if direction == "up" and index > 0:
            items[index - 1], items[index] = items[index], items[index - 1]
        elif direction == "down" and index < len(items) - 1:
            items[index], items[index + 1] = items[index + 1], items[index]
        else:
            return HttpResponse(status=204)

        with transaction.atomic():
            for pos, item in enumerate(items, start=1):
                RulesetAssertion.objects.filter(pk=item.pk).update(
                    order=pos * 10,
                )

        assertions = (
            default_ruleset.assertions.all()
            .select_related("target_signal_definition")
            .order_by("order", "pk")
        )
        if request.headers.get("HX-Request"):
            return render(
                request,
                "validations/library/partials/validator_default_assertions_card.html",
                {
                    "assertions": assertions,
                },
            )
        return redirect(
            reverse_with_org(
                "validations:validator_detail",
                request=request,
                kwargs={"slug": self.validator.slug},
            )
        )


class ValidatorRuleDeleteView(ValidatorRuleMixin, TemplateView):
    def post(self, request, *args, **kwargs):
        default_ruleset = self.validator.default_ruleset
        assertion = get_object_or_404(
            RulesetAssertion,
            pk=self.kwargs.get("rule_pk"),
            ruleset=default_ruleset,
        )
        assertion.delete()
        messages.success(request, _("Default assertion deleted."))
        if request.headers.get("HX-Request"):
            return self._hx_redirect()
        return self._redirect()


class ValidatorRuleListView(ValidatorRuleMixin, TemplateView):
    """Legacy list route redirects to the validator detail page."""

    def get(self, request, *args, **kwargs):
        return redirect(
            reverse_with_org(
                "validations:validator_detail",
                request=request,
                kwargs={"pk": self.validator.pk},
            ),
        )

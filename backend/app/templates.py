"""Extract bounded reporting fields without treating template text as instructions."""

from typing import Any

import yaml
from yaml.tokens import (
    AliasToken, AnchorToken, BlockEndToken, BlockMappingStartToken, BlockSequenceStartToken,
    FlowMappingEndToken, FlowMappingStartToken, FlowSequenceEndToken, FlowSequenceStartToken, TagToken,
)

from backend.app.models import IssueTemplate, TemplateField


def load_yaml(text: str) -> object:
    """Reject aliases, explicit tags and deep nesting before safe YAML construction."""
    if len(text.encode("utf-8")) > 32_768:
        raise ValueError("The template exceeds the supported byte limit.")
    depth = 0
    for token in yaml.scan(text):
        if isinstance(token, (AliasToken, AnchorToken, TagToken)):
            raise ValueError("YAML aliases, anchors and explicit tags are not supported.")
        if isinstance(token, (BlockMappingStartToken, BlockSequenceStartToken, FlowMappingStartToken, FlowSequenceStartToken)):
            depth += 1
            if depth > 32:
                raise ValueError("YAML nesting exceeds the supported depth.")
        elif isinstance(token, (BlockEndToken, FlowMappingEndToken, FlowSequenceEndToken)):
            depth -= 1
    return yaml.safe_load(text)


def parse_template(path: str, text: str, source_id: str) -> IssueTemplate:
    template = IssueTemplate(
        path=path, name=path.rsplit("/", 1)[-1], description="",
        format="markdown" if path.lower().endswith(".md") else "form", source_id=source_id,
    )
    if template.format == "markdown":
        _read_markdown(template, text)
    else:
        _read_form(template, text)
    return template


def _short(template: IssueTemplate, value: object, limit: int) -> str:
    """value cut to limit, noting a cut on the template; empty unless value is text."""
    if not isinstance(value, str):
        return ""
    if len(value) > limit:
        template.notes.append("Some extracted text was shortened; review the original template.")
    return value[:limit]


def _read_markdown(template: IssueTemplate, text: str) -> None:
    """Read the name and description from front matter.

    Markdown headings have no machine-readable required/optional semantics, so requirements stay unknown.
    """
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
        if end is not None:
            try:
                header = load_yaml("\n".join(lines[1:end]))
                if isinstance(header, dict):
                    template.name = _short(template, header.get("name"), 120) or template.name
                    template.description = _short(template, header.get("about"), 240)
            except (yaml.YAMLError, ValueError):
                template.notes.append("Markdown front matter could not be parsed safely.")


def _read_form(template: IssueTemplate, text: str) -> None:
    """Read a YAML issue form. Requirements are known only if the whole form was read without a note."""
    try:
        data = load_yaml(text)
    except (yaml.YAMLError, ValueError):
        template.notes.append("The YAML form could not be parsed safely; requirements remain unknown.")
        return
    if not isinstance(data, dict) or not isinstance(data.get("body"), list):
        template.notes.append("The YAML file does not contain a supported form body.")
        return
    template.name = _short(template, data.get("name"), 120) or template.name
    template.description = _short(template, data.get("description"), 240)
    for item in data["body"]:
        if not isinstance(item, dict):
            template.notes.append("An unsupported form element was omitted.")
            continue
        kind = item.get("type")
        if kind == "markdown":
            continue
        if not isinstance(kind, str) or kind not in {"input", "textarea", "dropdown", "checkboxes", "upload"}:
            template.notes.append("An unsupported input type was omitted.")
            continue
        if len(template.fields) >= 12:
            template.notes.append("Only the first twelve input fields were extracted.")
            break
        field = _form_field(template, kind, item)
        if field is not None:
            template.fields.append(field)
    template.notes = list(dict.fromkeys(template.notes))
    template.requirements_known = not template.notes


def _form_field(template: IssueTemplate, kind: str, item: dict[str, Any]) -> TemplateField | None:
    """One input of a form, or None with a note when it has no usable label."""
    attributes = item.get("attributes")
    if not isinstance(attributes, dict) or not isinstance(attributes.get("label"), str) or not attributes["label"].strip():
        template.notes.append("An input without a supported label was omitted.")
        return None
    validations = item.get("validations", {})
    raw_required = validations.get("required", False) if isinstance(validations, dict) else None
    required = raw_required if type(raw_required) is bool else None
    options: list[str] = []
    if kind == "checkboxes":
        options, flags_valid = _required_options(template, attributes.get("options"))
        if not flags_valid:
            required = None
    if required is None:
        template.notes.append("Some requirement flags were not valid booleans; their status is unknown.")
    return TemplateField(
        label=_short(template, attributes["label"], 120), description=_short(template, attributes.get("description"), 240),
        kind=kind, required=required, required_options=options,
    )


def _required_options(template: IssueTemplate, raw_options: object) -> tuple[list[str], bool]:
    """The labels of a checkbox input's required options, and whether every requirement flag was valid."""
    if not isinstance(raw_options, list):
        return [], False
    options: list[str] = []
    flags_valid = True
    for option in raw_options:
        if not isinstance(option, dict) or type(option.get("required", False)) is not bool:
            flags_valid = False
        elif option.get("required") is True:
            label = _short(template, option.get("label"), 160)
            if not label:
                flags_valid = False
            else:
                options.append(label)
    if len(options) > 6:
        template.notes.append("Only six required checkbox options were extracted.")
        options = options[:6]
    return options, flags_valid

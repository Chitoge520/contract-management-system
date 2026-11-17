"""Local masking of sensitive financial identifiers BEFORE any text leaves this computer.

Masked: bank account numbers, IBAN, SWIFT/BIC, CNAPS/routing/sort codes, bank name/address lines,
tax IDs, VAT numbers, company registration numbers, and any long bare digit run (>= 9 digits).
Kept: contract amounts (they contain thousand separators or decimals), dates, contract numbers.
"""
import re

MASK = "[REDACTED]"

# Labels whose value (rest of line after the label) is sensitive
LABELS = [
    r"bank\s*account(?:\s*(?:no\.?|number|name))?", r"account\s*(?:no\.?|number|#)", r"a/c\s*(?:no\.?)?",
    r"iban(?:\s*(?:no\.?|code))?", r"swift(?:\s*(?:code|/bic))?", r"bic(?:\s*code)?", r"cnaps(?:\s*code)?",
    r"routing\s*(?:no\.?|number)", r"sort\s*code", r"aba(?:\s*no\.?)?",
    r"bank\s*name", r"bank\s*address", r"beneficiary(?:'s)?\s*bank", r"payee\s*account(?:\s*name)?",
    r"tax\s*(?:id|identification)(?:\s*(?:no\.?|number))?", r"tax\s*(?:no\.?|number)", r"vat\s*(?:no\.?|number|id)",
    r"tin(?:\s*no\.?)?", r"company\s*(?:registration\s*)?(?:no\.?|number)", r"registration\s*(?:no\.?|number)",
    r"business\s*license\s*(?:no\.?|number)", r"unified\s*social\s*credit\s*code",
    "开户行", "开户银行", "银行账号", "账号", "帐号", "户名", "税号", "纳税人识别号", "统一社会信用代码", "注册号",
]
LABEL_RE = re.compile(r"(?i)(" + "|".join(
    (r"\b" + l + r"(?![A-Za-z])") if l[0].isascii() else l for l in LABELS) + r")(\s*[:：]?\s*)(.*)")

IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?\b")
LONG_DIGITS_RE = re.compile(r"(?<![\d,.])\d(?:[ -]?\d){8,}(?![\d,.])")
INLINE_ID_RE = re.compile(r"(?i)((?:private\s+)?company\s+no\.?|reg(?:istration)?\.?\s*no\.?|tax\s*id)\s*([A-Z0-9-]{5,})")


def redact_text(text: str):
    """Return (masked_text, count)."""
    count = 0
    out_lines = []
    pending_value = False  # label at end of previous line, value on this line
    for line in text.split("\n"):
        new = line
        if pending_value and new.strip():
            new = re.sub(r"\S.*", MASK, new, count=1)
            count += 1
            pending_value = False
        else:
            m = LABEL_RE.search(new)
            if m:
                value = m.group(3)
                if value.strip():
                    new = new[:m.start(3)] + MASK
                    count += 1
                else:
                    pending_value = True
        new, n = INLINE_ID_RE.subn(lambda mm: mm.group(1) + " " + MASK, new)
        count += n
        new, n = IBAN_RE.subn(MASK, new)
        count += n
        new, n = LONG_DIGITS_RE.subn(MASK, new)
        count += n
        out_lines.append(new)
    return "\n".join(out_lines), count

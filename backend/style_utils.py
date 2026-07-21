VALID_EFFECT_STYLES = {
    "blur",
    "soft-blur",
    "pixelate",
    "frosted",
    "smudge",
    "cover",
    "box",
    "black-box",
    "outline",
    "solid",
    "glitch",
    "rainbow",
    "dots",
    "scanline",
    "negative",
    "emboss",
}

ALIASES = {
    "soft": "soft-blur",
    "soft_blur": "soft-blur",
    "soft-blur": "soft-blur",
    "blackbox": "black-box",
    "black_box": "black-box",
    "black-box": "black-box",
    "solid_color": "solid",
    "solid-fill": "solid",
    "solid_fill": "solid",
}


def normalize_effect_style(style):
    if style is None:
        return "blur"

    normalized = str(style).strip().lower().replace("_", "-")
    normalized = normalized.replace(" ", "-")
    normalized = normalized.replace("--", "-")

    if not normalized:
        return "blur"

    if normalized in ALIASES:
        normalized = ALIASES[normalized]

    if normalized in VALID_EFFECT_STYLES:
        return normalized

    return "blur"

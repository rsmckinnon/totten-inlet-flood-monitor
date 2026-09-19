def build_risk(
    modeled_water_ft=None,
    flood_threshold_ft=None,
):
    """
    Assess coastal flood risk from the NOAA SSCOFS
    modeled water level relative to the property's
    provisional flooding threshold.

    Risk bands are intentionally conservative and
    provisional. They can be refined as local
    observations accumulate.

    HIGH:
        Forecast reaches or exceeds threshold.

    ELEVATED:
        Forecast is less than 0.5 ft below threshold.

    WATCH:
        Forecast is 0.5 to 1.5 ft below threshold.

    LOW:
        Forecast is more than 1.5 ft below threshold.
    """

    if modeled_water_ft is None:

        return {
            "level": "UNAVAILABLE",
            "margin_ft": None,
            "explanation":
                "NOAA water-level forecast unavailable.",
            "property_threshold_calibrated": False,
        }


    if flood_threshold_ft is None:

        return {
            "level": "UNAVAILABLE",
            "margin_ft": None,
            "explanation":
                "Property flooding threshold not set.",
            "property_threshold_calibrated": False,
        }


    margin = (
        modeled_water_ft -
        flood_threshold_ft
    )


    if margin >= 0:

        level = "HIGH"

        explanation = (
            "Forecast water level reaches or exceeds "
            "the property flooding threshold."
        )


    elif margin >= -0.5:

        level = "ELEVATED"

        explanation = (
            "Forecast water level is within 0.5 feet "
            "of the property flooding threshold."
        )


    elif margin >= -1.5:

        level = "WATCH"

        explanation = (
            "Forecast water level is within 1.5 feet "
            "of the property flooding threshold."
        )


    else:

        level = "LOW"

        explanation = (
            "Forecast water level remains more than "
            "1.5 feet below the property flooding "
            "threshold."
        )


    return {
        "level": level,
        "margin_ft": round(margin, 2),
        "modeled_water_ft":
            round(modeled_water_ft, 2),
        "flood_threshold_ft":
            round(flood_threshold_ft, 2),
        "explanation": explanation,
        "property_threshold_calibrated": True,
    }

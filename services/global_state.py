import hyperdiv as hd


@hd.global_state
class GlobalFilters(hd.BaseState):
    excluded_seasons = hd.Prop(hd.List(hd.String), [])
    # tuple[str] of "YYYY-YY" seasons to hide
    # Single-machine mode: always exactly one machine type.  ``app_context``
    # reconciles this on every render so it stays in sync with the user's
    # actual workouts (defaulting to "rower", or to the first available machine
    # when the user has no rower workouts).
    machine = hd.Prop(hd.String, "rower")

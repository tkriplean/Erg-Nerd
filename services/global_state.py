import hyperdiv as hd


@hd.global_state
class GlobalFilters(hd.BaseState):
    excluded_seasons = hd.Prop(hd.List(hd.String), [])
    # tuple[str] of "YYYY-YY" seasons to hide
    machine = hd.Prop(hd.String, "All")  # "All" or a machine-type string
    all_seasons = hd.Prop(hd.List(hd.String), [])
    all_machines = hd.Prop(hd.List(hd.String), [])

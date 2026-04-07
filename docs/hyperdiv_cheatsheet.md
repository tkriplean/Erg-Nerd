# HyperDiv Cheat Sheet


## App Structure

```python
import hyperdiv as hd

def main():
    hd.text("Hello World")

hd.run(main)
```

- Runs on `http://localhost:8888` by default (`HD_PORT` env var overrides)
- `main()` re-executes on every state change — treat it as a render function
- No build step: `python app.py`

---

## State Management

```python
# Reactive state — persists across re-runs
state = hd.state(count=0, name="Alice")
state.count += 1  # triggers re-render

# Component-scoped state
state = hd.local_state(expanded=False)
```

---

## Layout

```python
# Vertical (default)
with hd.box(gap=2, padding=1, align="center", justify="start"):
    ...

# Horizontal
with hd.hbox(gap=2):
    ...
```

Key props: `gap`, `padding`, `align`, `justify`, `width`, `height`, `grow`, `border`, `border_radius`, `background_color`, `vertical_scroll`

---

## Typography

```python
hd.h1("Heading") / hd.h2() / ... / hd.h6()
hd.text("Body text", font_size=1.5, font_weight="bold", font_color="blue-600")
hd.markdown("**Bold** and *italic*")
hd.code("print('hello')")
```

---

## Forms & Inputs

```python
btn = hd.button("Click", variant="primary")   # variants: default, primary, success, neutral, warning, danger
if btn.clicked: ...

ti = hd.text_input(placeholder="Enter text")
hd.text(ti.value)

sel = hd.select(placeholder="Choose:")
with sel:
    hd.option("Apple")
    hd.option("Banana")
hd.text(sel.value)

cb = hd.checkbox("Agree")
toggle = hd.switch("Enable")
slider = hd.slider(min_value=0, max_value=100, value=50)
ta = hd.textarea(rows=5)

# Form with validation
with hd.form() as form:
    form.text_input("Email:", name="email", required=True)
    form.submit_button("Submit")
if form.submitted:
    data = form.form_data  # {"email": "..."}
```

Check `.clicked`, `.changed`, `.value`, `.checked` on interactive components.

---

## Data Display

```python
# Auto-paginated table
hd.data_table(dict(Name=("Alice", "Bob"), Age=(30, 25)), rows_per_page=10)

hd.badge("New")
hd.tag("python", variant="primary")
hd.alert("Warning!", variant="warning", opened=True)   # MUST pass opened=True — alerts are hidden by default!  variants: info, success, warning, danger
with hd.card(): ...
```

---

## Navigation & Routing

```python
# Tabs
tabs = hd.tab_group("Overview", "History", "Settings")
if tabs.active == "Overview": ...

# Multi-page router (define at module level, outside main())
router = hd.router()

@router.route("/")
def home(): ...

@router.route("/users/{user_id}")
def user_detail(user_id): ...

def main():
    router.run()

# Programmatic navigation
loc = hd.location()
loc.path = "/users"
```

---

## Charts

```python
hd.line_chart((1,2,3,4,5), (5,4,3,2,1), labels=("A", "B"))
hd.bar_chart((10,20,15), labels=("Series",))
hd.pie_chart((30,25,45), labels=("A","B","C"))
hd.scatter_chart(((1,2),(2,3),(3,4)), labels=("Data",))

# Generic
hd.cartesian_chart("line", data, labels=("S",), y_min=0, y_max=100)
```

---

## Icons & Media

```python
hd.icon("gear", font_size=2, font_color="red")  # NOTE: use font_size, NOT size
if hd.icon_button("trash").clicked: ...
hd.image(src="url", width=20, height=15, border_radius="large")
```

---

## Theming

```python
theme = hd.theme()
theme.mode = "dark"                          # or "light"
theme.set_and_remember_theme_mode("dark")   # persists across sessions

# Color tokens: primary, success, warning, danger, neutral, + shades -50 to -900
# neutral-0 = white, neutral-1000 = black — do NOT use "white" or "black" directly
hd.box(background_color="primary-100")
hd.text("Text", font_color="neutral-600")
```

---

## Local Storage

```python
hd.local_storage.set_item("key", "value")

result = hd.local_storage.get_item("key")
if result.done:
    hd.text(result.result)   # None if not set

hd.local_storage.remove_item("key")
hd.local_storage.clear()
```

---

## Background Tasks

```python
def fetch_data():
    import time; time.sleep(2)
    return "loaded"

task = hd.task()
task.run(fetch_data)           # runs once, caches result

if task.running:
    hd.spinner()
elif task.done:
    if task.error:
        hd.alert(str(task.error), variant="danger")
    else:
        hd.text(task.result)

if hd.button("Reload").clicked:
    task.rerun(fetch_data)     # clears cache and re-runs
```

Supports both sync and `async def` functions.

---

## Common Patterns

```python
# Conditional rendering
if state.show:
    hd.text("Visible")

# Lists — use hd.scope() for unique keys so each item has isolated state
for item_id, item in items.items():
    with hd.scope(item_id):
        if hd.button(item["name"]).clicked: ...

# Dialog
state = hd.state(open=False)
if hd.button("Open").clicked:
    state.open = True
if state.open:
    with hd.dialog():
        hd.text("Content")
        if hd.button("Close").clicked:
            state.open = False

# Tooltip
with hd.tooltip("Helpful hint"):
    hd.button("Hover me")

# Dropdown menu
with hd.dropdown() as dd:
    hd.button("Menu")
    with dd.menu:
        hd.menu_item("Option 1")
        hd.menu_item("Delete", variant="danger")

# Loading gate (wait for async data before rendering)
result = hd.local_storage.get_item("token")
if not result.done:
    hd.spinner(); return
if not result.result:
    show_login(); return
show_app()
```


# HyperDiv Quirks & Workarounds

These are issues discovered during development that aren't obvious from docs:

## `radio_group` does not expose `size`
Shoelace's `sl-radio-group` supports `size`, but HyperDiv's wrapper doesn't declare it as a `Prop`. Passing `size=` to `hd.radio_group()` will raise an error.

**Workaround used in this project:** A local subclass adds the prop:
```python
class radio_group(hd.radio_group):
    size = hd.Prop(hd.OneOf("small", "medium", "large"), "medium")
```

## `StylePart` props require `hd.style()` objects
Props like `button_style`, `label_style`, `base_style` on Shoelace components are typed as `StylePart` and require `hd.style(...)` — not raw CSS strings.
```python
# Wrong:
hd.radio_button("X", button_style="font-size: 0.75rem")
# Right:
hd.radio_button("X", button_style=hd.style(font_size="0.75rem"))
```

## `BoxSize` does not accept CSS shorthand strings
`hd.style(padding="2px 8px")` will fail. Use a 4-tuple `(top, right, bottom, left)` or a single value:
```python
hd.style(padding=("2px", "8px", "2px", "8px"))  # explicit 4-tuple
hd.style(padding=1)                               # uniform, in rem
```

## `hd.option()` replaces spaces with underscores in its value
If you use `hd.option("Paul's Law")` inside an `hd.select()`, the internal value becomes `"Paul's_Law"`. Always use the `value=` kwarg for clean keys:
```python
hd.option("Paul's Law", value="pauls_law")
hd.option("Log-Log Watts Fit", value="loglog")
```
Then compare state against those short keys everywhere.

## `hd.dropdown()` accepts arbitrary content, not just menu items
Unlike `hd.select`, `hd.dropdown()` can contain any HyperDiv components in its body (checkboxes, buttons, boxes, etc.). Used in this project for the Events and Season filter pickers. Custom trigger via `slot=_dd.trigger`.

## `hd.task()` scope key determines lifetime
Changing the `hd.scope()` wrapping a task forces it to re-run. Used deliberately to re-trigger RowingLevel scrapes when profile or bests change:
```python
with hd.scope(f"rl_{profile_hash}_{bests_hash}"):
    task = hd.task()
    task.run(...)
```

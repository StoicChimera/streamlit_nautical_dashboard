"""
wip_labor_review.py
===================

Review tab UI for per-employee allocation.

Drill-down layout:
  - Left column: employee list with search + filter + one button per row
  - Right column: editor for the currently-selected employee

Only one editor renders at a time — widget registration stays constant
regardless of total employee count. Scales to 200+ employees cleanly.

A "Multi-select for bulk approve" toggle in the employee list switches
the row interaction from "click to drill" to "check to include in bulk
batch". When on, the bulk-assign panel above the list becomes the active
workspace: define a role + allocation template, then apply it to every
checked employee in one transaction. Pattern matches the addon bulk-alias
flow on the tax preflight page.

Performance: the three big render blocks — employee list, bulk panel,
per-employee editor — are each wrapped in @st.fragment so that
intra-fragment interactions (search typing, building template lines,
editing one employee's allocation) only rerun the relevant fragment
instead of the whole script. Cross-fragment events (toggling multi
mode, applying a batch, approving an individual) explicitly call
st.rerun(scope="app") to refresh the other fragments.

Exports one public function:
    render_review_tab(period, labor_source, reviewer_name)
"""

import pandas as pd
import streamlit as st

from . import wip_labor_allocation as wla


# -------------------------------------------------------------
# Helpers
# -------------------------------------------------------------

def _dollar(v):
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _dollar_or_hidden(v, show_amounts: bool):
    """When show_amounts=False, render the dollar field as a placeholder
    so managers/supervisors can review allocation without seeing pay."""
    if not show_amounts:
        return "—"
    return _dollar(v)


def _state_key(period, labor_source, employee, suffix):
    return f"alloc_{period}_{labor_source}_{employee}_{suffix}"


def _selected_key(period, labor_source):
    return f"selected_{period}_{labor_source}"


def _bulk_multi_key(period, labor_source):
    return f"bulk_multi_{period}_{labor_source}"


def _bulk_set_key(period, labor_source):
    return f"bulk_selected_set_{period}_{labor_source}"


def _empty_line() -> dict:
    return {
        'line_type':            'direct_program',
        'target_program':       None,
        'cost_center_name':     None,
        'allocation_pct':       0.0,
        'program_restrictions': None,
    }


def _load_into_state(period: str, labor_source: str, employee: str):
    """Load allocation into session state on first editor open for this employee."""
    role_k   = _state_key(period, labor_source, employee, 'role')
    lines_k  = _state_key(period, labor_source, employee, 'lines')
    loaded_k = _state_key(period, labor_source, employee, 'loaded')
    source_k = _state_key(period, labor_source, employee, 'carry_source')

    if st.session_state.get(loaded_k):
        return

    current = wla.get_employee_allocation(period, employee, labor_source)
    if current:
        st.session_state[role_k]   = current['role_name']
        st.session_state[lines_k]  = [dict(ln) for ln in current['lines']]
        st.session_state[source_k] = None
    else:
        prior = wla.get_prior_period_allocation(period, employee, labor_source)
        if prior:
            st.session_state[role_k]   = prior['role_name']
            st.session_state[lines_k]  = [dict(ln) for ln in prior['lines']]
            st.session_state[source_k] = prior['source_period']
        else:
            st.session_state[role_k]   = ""
            st.session_state[lines_k]  = []
            st.session_state[source_k] = None

    st.session_state[loaded_k] = True


def _clear_state(period: str, labor_source: str, employee: str):
    """Clear session state so the next editor open reloads from DB."""
    for suffix in ('role', 'lines', 'loaded', 'carry_source'):
        k = _state_key(period, labor_source, employee, suffix)
        if k in st.session_state:
            del st.session_state[k]


def _row_label(row: pd.Series, show_amounts: bool = False) -> str:
    """Compact single-line label for the row button."""
    name = str(row['employee_name'])
    cost = _dollar_or_hidden(row['total_labor_cost'], show_amounts)
    reviewed = bool(row['reviewed']) if pd.notna(row['reviewed']) else False
    is_new = bool(row.get('is_new_employee', False)) if 'is_new_employee' in row else False

    status = "Approved" if reviewed else "Pending"
    new_tag = " NEW" if is_new else ""
    return f"[{status}]{new_tag}  {name}  ·  {cost}"


def _render_row_button(row: pd.Series, period: str, labor_source: str,
                       show_amounts: bool, currently_selected):
    """Single-mode row renderer. Click selects the employee for the editor
    in the right-hand column — that lives in a separate fragment, so we
    force an app-scoped rerun to make the editor update."""
    name = row['employee_name']
    is_selected = (name == currently_selected)
    btn_key = f"row_btn_{period}_{labor_source}_{name}"
    if st.button(
        _row_label(row, show_amounts),
        key=btn_key,
        type="primary" if is_selected else "secondary",
        use_container_width=True,
    ):
        st.session_state[_selected_key(period, labor_source)] = name
        st.rerun(scope="app")  # editor lives in another fragment


def _render_row_checkbox(row: pd.Series, period: str, labor_source: str,
                          show_amounts: bool, bulk_set_key: str):
    """Multi-select row renderer used when bulk-approve mode is on.

    The checkbox state is mirrored into st.session_state[bulk_set_key]
    (a set of employee_name strings). Caller is responsible for filtering
    out approved rows — this function does not check reviewed status.

    A change here forces an app-scoped rerun so the bulk panel above the
    list (in another fragment) updates its 'N checked' header and the
    Apply button label/disabled state."""
    name = row['employee_name']
    cur_set = st.session_state.get(bulk_set_key, set())
    is_checked = name in cur_set
    chk_key = f"row_chk_{period}_{labor_source}_{name}"

    new_checked = st.checkbox(
        _row_label(row, show_amounts),
        value=is_checked,
        key=chk_key,
    )
    if new_checked != is_checked:
        if new_checked:
            cur_set = cur_set | {name}
        else:
            cur_set = cur_set - {name}
        st.session_state[bulk_set_key] = cur_set
        st.rerun(scope="app")  # bulk panel header reads from this set


# -------------------------------------------------------------
# Left column — employee list  (FRAGMENT)
# -------------------------------------------------------------

@st.fragment
def _render_employee_list(period: str, labor_source: str, employees: pd.DataFrame, show_amounts: bool = False):
    """Renders the filterable employee list as its own fragment.

    Intra-fragment interactions (search typing, filter radio, scrolling)
    only rerun this function — the bulk panel above and the editor on
    the right stay untouched. Interactions that DO need a cross-fragment
    update (multi toggle, bulk select-all controls, individual row
    checkbox/click) explicitly call st.rerun(scope="app") to refresh
    the other fragments.
    """
    multi_key    = _bulk_multi_key(period, labor_source)
    bulk_set_key = _bulk_set_key(period, labor_source)
    if bulk_set_key not in st.session_state:
        st.session_state[bulk_set_key] = set()

    sel_k = _selected_key(period, labor_source)
    currently_selected = st.session_state.get(sel_k)

    period_committed = wla.is_period_committed(period)

    # Mode toggle — change requires app-scoped rerun so the bulk panel
    # (other fragment) flips between collapsed expander and full panel.
    prior_multi = st.session_state.get(multi_key, False)
    multi = st.checkbox(
        "Multi-select for bulk approve",
        value=prior_multi,
        key=f"multi_toggle_widget_{period}_{labor_source}",
        help=(
            "Check multiple employees and apply the same role + allocation "
            "template to all at once via the bulk panel above the list."
        ),
        disabled=period_committed,
    )
    st.session_state[multi_key] = multi
    if multi != prior_multi:
        st.rerun(scope="app")

    # Filters — pure list-fragment concerns, auto-rerun stays scoped here.
    search_term = st.text_input(
        "Search",
        key=f"search_{period}_{labor_source}",
        placeholder="Employee name...",
        label_visibility="collapsed",
    )
    filter_choice = st.radio(
        "Show",
        options=["All", "Not allocated", "Approved"],
        horizontal=True,
        key=f"review_filter_{period}_{labor_source}",
        label_visibility="collapsed",
    )

    df = employees.copy()
    if 'is_new_employee' not in df.columns:
        df['is_new_employee'] = False

    if search_term:
        df = df[df['employee_name'].str.contains(search_term, case=False, na=False)]
    if filter_choice == "Not allocated":
        df = df[~df['reviewed'].fillna(False)]
    elif filter_choice == "Approved":
        df = df[df['reviewed'].fillna(False) == True]

    n_selected = len(st.session_state.get(bulk_set_key, set()))
    if multi:
        st.caption(f"Showing {len(df)} of {len(employees)}  ·  **{n_selected}** checked")
    else:
        st.caption(f"Showing {len(df)} of {len(employees)}")

    if df.empty:
        st.info("No employees match the current filter.")
        return

    # Multi-mode bulk selection controls. These mutate the shared
    # bulk_set_key, which the bulk panel reads — needs app-scoped rerun.
    if multi:
        unreviewed_visible = df[~df['reviewed'].fillna(False)]
        n_visible_unrev = len(unreviewed_visible)

        bc1, bc2, bc3 = st.columns(3)
        with bc1:
            if st.button(
                f"Select visible ({n_visible_unrev})",
                key=f"sel_all_visible_{period}_{labor_source}",
                use_container_width=True,
                disabled=n_visible_unrev == 0,
                help="Add every unreviewed employee currently visible to the bulk batch.",
            ):
                cur = st.session_state.get(bulk_set_key, set())
                cur |= set(unreviewed_visible['employee_name'])
                st.session_state[bulk_set_key] = cur
                st.rerun(scope="app")
        with bc2:
            if st.button(
                "Uncheck visible",
                key=f"clear_visible_{period}_{labor_source}",
                use_container_width=True,
                disabled=n_selected == 0,
                help="Remove visible employees from the bulk batch.",
            ):
                cur = st.session_state.get(bulk_set_key, set())
                cur -= set(df['employee_name'])
                st.session_state[bulk_set_key] = cur
                st.rerun(scope="app")
        with bc3:
            if st.button(
                "Clear all",
                key=f"clear_all_{period}_{labor_source}",
                use_container_width=True,
                disabled=n_selected == 0,
                help="Empty the bulk batch.",
            ):
                st.session_state[bulk_set_key] = set()
                st.rerun(scope="app")

    is_new      = df['is_new_employee'].fillna(False) == True
    is_reviewed = df['reviewed'].fillna(False) == True

    new_hires_pending    = df[is_new  & ~is_reviewed]
    returning_unreviewed = df[~is_new & ~is_reviewed]
    all_approved         = df[is_reviewed]

    with st.container(height=560):
        if not new_hires_pending.empty:
            st.markdown(f"**New hires — pending approval ({len(new_hires_pending)})** — no prior allocation")
            for _, row in new_hires_pending.iterrows():
                if multi:
                    _render_row_checkbox(row, period, labor_source, show_amounts, bulk_set_key)
                else:
                    _render_row_button(row, period, labor_source, show_amounts, currently_selected)
            st.markdown("")

        if not returning_unreviewed.empty:
            st.markdown(f"**Returning — pending approval ({len(returning_unreviewed)})**")
            for _, row in returning_unreviewed.iterrows():
                if multi:
                    _render_row_checkbox(row, period, labor_source, show_amounts, bulk_set_key)
                else:
                    _render_row_button(row, period, labor_source, show_amounts, currently_selected)
            st.markdown("")

        if not all_approved.empty:
            st.markdown(f"**Approved ({len(all_approved)})**")
            for _, row in all_approved.iterrows():
                if multi:
                    # Approved employees are out of scope for bulk apply —
                    # render as static greyed labels so the user still sees
                    # progress against headcount but can't accidentally
                    # overwrite an approved allocation in a bulk batch.
                    st.markdown(
                        f"<div style='padding:4px 8px;color:#888;font-size:0.85em'>{_row_label(row, show_amounts)}</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    _render_row_button(row, period, labor_source, show_amounts, currently_selected)


# -------------------------------------------------------------
# Bulk assign + approve panel  (FRAGMENT)
# -------------------------------------------------------------

@st.fragment
def _render_bulk_assign_panel(
    period: str, labor_source: str,
    employees: pd.DataFrame, reviewer_name: str,
):
    """Bulk-apply a role + allocation template to many unreviewed employees.

    Two render states:
      - Multi-select OFF: collapsed expander with a hint about turning on
        multi-select mode in the employee list below.
      - Multi-select ON: full panel with role selector, line builder, and an
        Apply button that reads the checked employees from session state.

    Same line contract as the per-employee editor — supports direct_program
    lines, cost_center lines, percentage splits, and program restrictions.
    Lines must sum to 100% before the apply button enables.

    Fragment-scoped: building lines, adding/removing lines, and editing
    template widgets only rerun this panel. The Apply button on success
    forces an app-scoped rerun so the list and KPIs reflect new approvals.
    """
    if wla.is_period_committed(period):
        return

    multi_key    = _bulk_multi_key(period, labor_source)
    bulk_set_key = _bulk_set_key(period, labor_source)
    if bulk_set_key not in st.session_state:
        st.session_state[bulk_set_key] = set()

    multi = st.session_state.get(multi_key, False)

    if not multi:
        with st.expander("Bulk assign and approve", expanded=False):
            st.caption(
                "Turn on **Multi-select for bulk approve** in the employee list "
                "below to start. Check the employees you want, define a role + "
                "allocation template here, then apply it to all of them in one "
                "transaction."
            )
        return

    role_k  = f"bulk_role_{period}_{labor_source}"
    lines_k = f"bulk_lines_{period}_{labor_source}"

    if lines_k not in st.session_state:
        st.session_state[lines_k] = []

    selected_set = st.session_state.get(bulk_set_key, set())
    n_selected   = len(selected_set)

    with st.container(border=True):
        # Header — selection summary
        if n_selected == 0:
            st.markdown("### Bulk assign and approve")
            st.caption(
                "No employees checked yet — define the template here, then check "
                "rows in the list below. The Apply button enables once you have "
                "a valid template + at least one employee."
            )
        else:
            preview = sorted(selected_set)[:5]
            preview_str = ", ".join(f"`{p}`" for p in preview)
            if n_selected > 5:
                preview_str += f" + {n_selected - 5} more"
            plural = "s" if n_selected != 1 else ""
            st.markdown(f"### Bulk assign and approve — {n_selected} employee{plural} checked")
            st.caption(preview_str)

        st.markdown("")

        # ---- Role ----
        roles_df = wla.get_available_roles()
        role_options = [""] + roles_df['role_name'].tolist()
        current_role = st.session_state.get(role_k, "")
        role_idx = role_options.index(current_role) if current_role in role_options else 0
        selected_role = st.selectbox(
            "Canonical role",
            options=role_options,
            index=role_idx,
            key=f"bulk_role_widget_{period}_{labor_source}",
        )
        st.session_state[role_k] = selected_role

        if selected_role:
            role_row = roles_df[roles_df['role_name'] == selected_role]
            if not role_row.empty:
                ct = str(role_row.iloc[0]['cost_type'])
                st.caption(f"Cost type: **{ct}**")

        # ---- Lines ----
        st.markdown("**Allocation lines**")

        cc_df        = wla.get_available_cost_centers()
        cc_options   = [""] + cc_df['cost_center_name'].tolist()
        all_programs = [""] + wla.get_all_revenue_programs()

        lines = st.session_state.get(lines_k, [])

        for i, line in enumerate(lines):
            type_k = f"bulk_type_{period}_{labor_source}_{i}"
            tp_k   = f"bulk_tp_{period}_{labor_source}_{i}"
            cc_k   = f"bulk_cc_{period}_{labor_source}_{i}"
            pr_k   = f"bulk_pr_{period}_{labor_source}_{i}"
            pct_k  = f"bulk_pct_{period}_{labor_source}_{i}"
            rm_k   = f"bulk_rm_{period}_{labor_source}_{i}"

            st.markdown(f"*Line {i+1}*")
            col_type, col_target, col_pct, col_rm = st.columns([2, 5, 2, 1])

            with col_type:
                lt_current = line.get('line_type', 'direct_program')
                lt = st.selectbox(
                    "Type",
                    options=['direct_program', 'cost_center'],
                    index=0 if lt_current == 'direct_program' else 1,
                    key=type_k,
                    label_visibility='collapsed',
                )

            with col_target:
                if lt == 'direct_program':
                    current_tp = line.get('target_program') or ""
                    tp_idx = all_programs.index(current_tp) if current_tp in all_programs else 0
                    tp = st.selectbox(
                        "Target program",
                        options=all_programs,
                        index=tp_idx,
                        key=tp_k,
                        label_visibility='collapsed',
                    )
                    lines[i] = {
                        'line_type':            'direct_program',
                        'target_program':       tp if tp else None,
                        'cost_center_name':     None,
                        'allocation_pct':       float(line.get('allocation_pct', 0)),
                        'program_restrictions': None,
                    }
                else:
                    current_cc = line.get('cost_center_name') or ""
                    cc_idx = cc_options.index(current_cc) if current_cc in cc_options else 0
                    cc = st.selectbox(
                        "Cost center",
                        options=cc_options,
                        index=cc_idx,
                        key=cc_k,
                        label_visibility='collapsed',
                    )
                    restrictions = []
                    if cc:
                        try:
                            eligible = wla.get_programs_for_cost_center(cc, period)
                        except ValueError:
                            eligible = []
                        current_restrictions = line.get('program_restrictions') or []
                        restrictions = st.multiselect(
                            "Restrict driver to (optional)",
                            options=eligible,
                            default=[r for r in current_restrictions if r in eligible],
                            key=pr_k,
                        )
                    lines[i] = {
                        'line_type':            'cost_center',
                        'target_program':       None,
                        'cost_center_name':     cc if cc else None,
                        'allocation_pct':       float(line.get('allocation_pct', 0)),
                        'program_restrictions': restrictions if restrictions else None,
                    }

            with col_pct:
                pct_display = float(line.get('allocation_pct', 0)) * 100
                pct = st.number_input(
                    "%", min_value=0.0, max_value=100.0, step=5.0,
                    value=pct_display, format="%.2f",
                    key=pct_k, label_visibility='collapsed',
                )
                lines[i]['allocation_pct'] = pct / 100.0

            with col_rm:
                # Fragment-scoped rerun — only the bulk panel's lines change.
                if st.button("Remove", key=rm_k, help="Remove this line"):
                    lines.pop(i)
                    st.session_state[lines_k] = lines
                    st.rerun()

        st.session_state[lines_k] = lines

        total = sum(float(ln.get('allocation_pct', 0)) for ln in lines)
        is_100 = abs(total - 1.0) < 1e-6

        col_add, col_total = st.columns([1, 3])
        with col_add:
            # Fragment-scoped rerun — only the bulk panel's lines change.
            if st.button(
                "+ Add line",
                key=f"bulk_add_line_{period}_{labor_source}",
            ):
                lines.append(_empty_line())
                st.session_state[lines_k] = lines
                st.rerun()
        with col_total:
            if not lines:
                st.info("Add at least one allocation line.")
            elif is_100:
                st.success(f"Total: {total*100:.2f}%")
            else:
                st.warning(f"Total: {total*100:.2f}% — must equal 100% to apply.")

        # ---- Apply ----
        st.markdown("")
        can_apply = (
            bool(selected_role)
            and is_100
            and n_selected > 0
            and bool(reviewer_name and reviewer_name.strip())
        )

        apply_label = (
            f"Apply to {n_selected} checked and approve"
            if n_selected > 0 else "Apply to checked and approve"
        )

        if st.button(
            apply_label,
            key=f"bulk_apply_btn_{period}_{labor_source}",
            type="primary",
            use_container_width=True,
            disabled=not can_apply,
        ):
            try:
                result = wla.bulk_apply_allocation(
                    period=period,
                    labor_source=labor_source,
                    employees=sorted(selected_set),
                    role_name=selected_role,
                    lines=lines,
                    reviewer_name=reviewer_name,
                )
                msg = f"Applied + approved {result['applied']} of {result['total']}."
                if result['skipped']:
                    st.warning(msg + " See expander for skipped employees.")
                    with st.expander(f"Skipped ({len(result['skipped'])})", expanded=False):
                        st.dataframe(
                            pd.DataFrame(result['skipped']),
                            use_container_width=True,
                            hide_index=True,
                        )
                else:
                    st.success(msg)
                # Clear the checked set so the same template doesn't re-apply
                # by accident on the next batch. Leave multi mode on and
                # leave the role + lines template alone so the user can keep
                # batching with a tweaked role.
                st.session_state[bulk_set_key] = set()
                # App-scoped rerun — list + KPIs need to reflect new approvals.
                st.rerun(scope="app")
            except ValueError as e:
                st.error(str(e))


# -------------------------------------------------------------
# Right column — editor body  (FRAGMENT)
# -------------------------------------------------------------

@st.fragment
def _render_editor(period: str, labor_source: str, employee: str, reviewer_name: str,
                   current_row: pd.Series, show_amounts: bool = False):
    """Renders the editor for the selected employee as its own fragment.

    Per-line widget edits (role selectbox, line type, target, percentage,
    restrictions) only rerun this panel. Approve/Unapprove forces an
    app-scoped rerun so the list and KPIs reflect the new state.
    """
    # Gate: if the period's allocation is already committed, edits would silently
    # invalidate the locked snapshot. Show a read-only summary and a clear
    # instruction to unlock first.
    if wla.is_period_committed(period):
        reviewed = bool(current_row['reviewed']) if pd.notna(current_row['reviewed']) else False
        status_badge = "Approved" if reviewed else "Not allocated"
        st.markdown(
            f"### {employee}  \n"
            f"{_dollar_or_hidden(current_row['total_labor_cost'], show_amounts)}  ·  {status_badge}"
        )
        st.warning(
            f"Period {period} allocation is **committed**. Editing employee "
            "allocations now would silently invalidate the locked snapshot in "
            "stg_labor_applied and the profitability MV. "
            "Unlock the period from the Allocation tab before making changes."
        )
        # Read-only display of the current allocation, if any
        current = wla.get_employee_allocation(period, employee, labor_source)
        if current and current.get('lines'):
            st.markdown(f"**Role:** {current['role_name']}")
            st.markdown("**Current allocation lines:**")
            display_rows = []
            for ln in current['lines']:
                display_rows.append({
                    "Type":        ln['line_type'],
                    "Target":      ln.get('target_program') or ln.get('cost_center_name') or "",
                    "%":           f"{float(ln['allocation_pct']) * 100:.2f}%",
                    "Restrictions": ", ".join(ln.get('program_restrictions') or []) or "—",
                })
            import pandas as _pd
            st.dataframe(_pd.DataFrame(display_rows), use_container_width=True, hide_index=True)
        else:
            st.caption("No allocation on file for this employee.")
        return

    _load_into_state(period, labor_source, employee)

    role_k   = _state_key(period, labor_source, employee, 'role')
    lines_k  = _state_key(period, labor_source, employee, 'lines')
    source_k = _state_key(period, labor_source, employee, 'carry_source')

    # Header — name + cost + status
    reviewed = bool(current_row['reviewed']) if pd.notna(current_row['reviewed']) else False
    status_badge = "✓ Approved" if reviewed else "⚠ Not allocated"
    st.markdown(
            f"### {employee}  \n"
            f"{_dollar_or_hidden(current_row['total_labor_cost'], show_amounts)}  ·  {status_badge}"
        )

    # Carry-forward notice
    if st.session_state.get(source_k):
        st.info(f"Carried forward from {st.session_state[source_k]} — review and approve.")

    # UKG context
    ukg_prog = str(current_row.get('ukg_program') or '')
    ukg_role = str(current_row.get('ukg_role') or '')
    if ukg_prog or ukg_role:
        st.caption(f"UKG context — program: {ukg_prog}  ·  role: {ukg_role}")

    st.markdown("")

    # Role dropdown
    roles_df = wla.get_available_roles()
    role_options = [""] + roles_df['role_name'].tolist()
    current_role = st.session_state.get(role_k, "")
    role_idx = role_options.index(current_role) if current_role in role_options else 0
    selected_role = st.selectbox(
        "Canonical role",
        options=role_options,
        index=role_idx,
        key=_state_key(period, labor_source, employee, 'role_widget'),
    )
    st.session_state[role_k] = selected_role

    # Role cost_type hint
    if selected_role:
        role_row = roles_df[roles_df['role_name'] == selected_role]
        if not role_row.empty:
            ct = str(role_row.iloc[0]['cost_type'])
            is_direct = bool(role_row.iloc[0]['is_direct_assignment'])
            hint = f"Cost type: **{ct}**"
            if is_direct:
                dp = role_row.iloc[0]['direct_assignment_program']
                hint += "  ·  Direct assignment role"
                if pd.notna(dp):
                    hint += f" (default program: {dp})"
            st.caption(hint)

    st.markdown("**Allocation lines**")

    cc_df = wla.get_available_cost_centers()
    cc_options = [""] + cc_df['cost_center_name'].tolist()
    all_programs = [""] + wla.get_all_revenue_programs()

    lines = st.session_state.get(lines_k, [])

    # Render each line
    for i, line in enumerate(lines):
        line_type_k = _state_key(period, labor_source, employee, f'type_{i}')
        tp_k        = _state_key(period, labor_source, employee, f'tp_{i}')
        cc_k        = _state_key(period, labor_source, employee, f'cc_{i}')
        pr_k        = _state_key(period, labor_source, employee, f'pr_{i}')
        pct_k       = _state_key(period, labor_source, employee, f'pct_{i}')
        rm_k        = _state_key(period, labor_source, employee, f'rm_{i}')

        st.markdown(f"*Line {i+1}*")
        col_type, col_target, col_pct, col_rm = st.columns([2, 5, 2, 1])

        with col_type:
            lt_current = line.get('line_type', 'direct_program')
            lt = st.selectbox(
                "Type",
                options=['direct_program', 'cost_center'],
                index=0 if lt_current == 'direct_program' else 1,
                key=line_type_k,
                label_visibility='collapsed',
            )

        with col_target:
            if lt == 'direct_program':
                current_tp = line.get('target_program') or ""
                tp_idx = all_programs.index(current_tp) if current_tp in all_programs else 0
                tp = st.selectbox(
                    "Target program",
                    options=all_programs,
                    index=tp_idx,
                    key=tp_k,
                    label_visibility='collapsed',
                )
                lines[i] = {
                    'line_type':            'direct_program',
                    'target_program':       tp if tp else None,
                    'cost_center_name':     None,
                    'allocation_pct':       float(line.get('allocation_pct', 0)),
                    'program_restrictions': None,
                }
            else:
                current_cc = line.get('cost_center_name') or ""
                cc_idx = cc_options.index(current_cc) if current_cc in cc_options else 0
                cc = st.selectbox(
                    "Cost center",
                    options=cc_options,
                    index=cc_idx,
                    key=cc_k,
                    label_visibility='collapsed',
                )
                restrictions = []
                if cc:
                    try:
                        eligible = wla.get_programs_for_cost_center(cc, period)
                    except ValueError:
                        eligible = []
                    current_restrictions = line.get('program_restrictions') or []
                    restrictions = st.multiselect(
                        "Restrict driver to (optional)",
                        options=eligible,
                        default=[r for r in current_restrictions if r in eligible],
                        key=pr_k,
                    )
                lines[i] = {
                    'line_type':            'cost_center',
                    'target_program':       None,
                    'cost_center_name':     cc if cc else None,
                    'allocation_pct':       float(line.get('allocation_pct', 0)),
                    'program_restrictions': restrictions if restrictions else None,
                }

        with col_pct:
            pct_display = float(line.get('allocation_pct', 0)) * 100
            pct = st.number_input(
                "%",
                min_value=0.0,
                max_value=100.0,
                step=5.0,
                value=pct_display,
                format="%.2f",
                key=pct_k,
                label_visibility='collapsed',
            )
            lines[i]['allocation_pct'] = pct / 100.0

        with col_rm:
            # Fragment-scoped — only the editor's lines change.
            if st.button("✕", key=rm_k, help="Remove this line"):
                lines.pop(i)
                st.session_state[lines_k] = lines
                st.rerun()

    st.session_state[lines_k] = lines

    # Total + add line
    total = sum(float(ln.get('allocation_pct', 0)) for ln in lines)
    is_100 = abs(total - 1.0) < 1e-6

    col_add, col_total = st.columns([1, 3])
    with col_add:
        # Fragment-scoped — only the editor's lines change.
        if st.button(
            "+ Add line",
            key=_state_key(period, labor_source, employee, 'add_line'),
        ):
            lines.append(_empty_line())
            st.session_state[lines_k] = lines
            st.rerun()
    with col_total:
        if is_100:
            st.success(f"Total: {total*100:.2f}%")
        else:
            st.warning(f"Total: {total*100:.2f}% — must equal 100% to approve")

    # Approve / Unapprove — both are cross-fragment events (list + KPIs need
    # to reflect the new reviewed state) so they force an app-scoped rerun.
    col_approve, col_unapprove, _ = st.columns([1, 1, 2])

    with col_approve:
        if st.button(
            "Approve",
            key=_state_key(period, labor_source, employee, 'btn_approve'),
            type="primary",
            disabled=not is_100 or not selected_role or reviewed,
        ):
            if not reviewer_name.strip():
                st.error("Enter your name in the Reviewer's Name field above.")
            else:
                try:
                    wla.save_employee_allocation(
                        period, employee, labor_source,
                        selected_role, lines, reviewer_name,
                    )
                    wla.mark_employee_reviewed(
                        period, employee, labor_source, reviewer_name,
                    )
                    _clear_state(period, labor_source, employee)
                    st.rerun(scope="app")
                except ValueError as e:
                    st.error(str(e))

    with col_unapprove:
        if st.button(
            "Unapprove",
            key=_state_key(period, labor_source, employee, 'btn_unapprove'),
            type="secondary",
            disabled=not reviewed,
        ):
            wla.unmark_employee_reviewed(period, employee, labor_source)
            _clear_state(period, labor_source, employee)
            st.rerun(scope="app")


# -------------------------------------------------------------
# Public entry point
# -------------------------------------------------------------

def render_review_tab(period: str, labor_source: str, reviewer_name: str, show_amounts: bool = False):
    """Main review tab. labor_source is 'direct' or 'temp'.

    Top-level (KPIs, carry-forward bulk button) is NOT a fragment — those
    bits are cheap to render and need to update on every approval anyway.
    The three heavy panels — bulk assign, employee list, per-employee editor
    — each live in their own fragment for scoped reruns. See module docstring.
    """
    if labor_source not in ('direct', 'temp'):
        st.error(f"Invalid labor_source: {labor_source!r}")
        return

    employees = wla.list_employees_for_review(period, labor_source)
    if employees.empty:
        st.info(f"No {labor_source} labor data for {period}.")
        return

    if wla.is_period_committed(period):
        st.warning(
            f"Period {period} allocation is **committed**. Approve, Unapprove, "
            "and line edits are disabled for this period. To make changes, go "
            "to the Allocation tab and click **Unlock and Recommit** first."
        )

    # --- Top-level summary ---
    total_employees = len(employees)
    reviewed_count  = int(employees['reviewed'].fillna(False).sum())
    not_allocated   = total_employees - reviewed_count
    total_cost      = float(employees['total_labor_cost'].sum())

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Employees", total_employees)
    k2.metric("Approved", reviewed_count)
    k3.metric("Not allocated", not_allocated)
    k4.metric("Total labor", _dollar_or_hidden(total_cost, show_amounts))

    pct_done = reviewed_count / total_employees if total_employees else 0
    st.progress(pct_done, text=f"{reviewed_count} of {total_employees} approved ({pct_done:.0%})")

    # Bulk approve carried-forward employees (one-click; separate from
    # the template-based bulk panel below). Lives at top level (not in
    # a fragment) so the existing st.rerun() forces a full app rerun
    # which is what we want — list and KPIs both refresh.
    if not wla.is_period_committed(period):
        eligible_mask = (
            (~employees['is_new_employee'].fillna(False))
            & (~employees['reviewed'].fillna(False))
            & (employees['line_count'].fillna(0) == 0)
        )
        eligible_count = int(eligible_mask.sum())
        if eligible_count > 0:
            col_btn, col_msg = st.columns([2, 5])
            with col_btn:
                if st.button(
                    f"Carry forward + approve {eligible_count} returning",
                    key=f"bulk_carry_{period}_{labor_source}",
                    type="primary",
                    use_container_width=True,
                    disabled=not reviewer_name.strip(),
                ):
                    result = wla.bulk_approve_carried_forward(
                        period, labor_source, reviewer_name,
                    )
                    msg = (
                        f"Approved {result['approved']}. "
                        f"Skipped {len(result['skipped'])}. "
                        f"No prior allocation: {result['no_prior']}."
                    )
                    if result['skipped']:
                        st.warning(msg + " See expander for skipped employees.")
                        with st.expander(f"Skipped ({len(result['skipped'])})", expanded=False):
                            st.dataframe(
                                pd.DataFrame(result['skipped']),
                                use_container_width=True,
                                hide_index=True,
                            )
                    else:
                        st.success(msg)
                    st.rerun()
            with col_msg:
                st.caption(
                    "Copies prior period role + lines, validates, and marks reviewed. "
                    "Anyone whose role/program/cost center is no longer active, or "
                    "whose lines don't sum to 100%, lands in 'pending approval' for "
                    "individual review."
                )

    # Bulk assign + approve (FRAGMENT) — collapsed expander when multi mode
    # is off, full bordered panel when on. See _render_bulk_assign_panel.
    _render_bulk_assign_panel(period, labor_source, employees, reviewer_name)

    st.markdown("")

    # --- Drill-down layout ---
    col_list, col_editor = st.columns([2, 3], gap="medium")

    with col_list:
        # FRAGMENT — search/filter/scrolling stay scoped here.
        _render_employee_list(period, labor_source, employees, show_amounts)

    with col_editor:
        # When multi-select mode is on, the right-hand drill-down is
        # superseded by the bulk panel above. Show a clear hint instead
        # of the editor — same pattern as the preflight addon review tab.
        multi_key = _bulk_multi_key(period, labor_source)
        if st.session_state.get(multi_key, False):
            st.info(
                "Multi-select mode is on. Use the **Bulk assign and approve** "
                "panel above to apply a role + allocation template to all "
                "checked employees at once. Switch off **Multi-select for "
                "bulk approve** in the employee list to drill into a single "
                "employee."
            )
            return

        sel_k = _selected_key(period, labor_source)
        selected = st.session_state.get(sel_k)

        if not selected:
            st.info("← Pick an employee from the list to start allocating.")
            return

        row_match = employees[employees['employee_name'] == selected]
        if row_match.empty:
            st.warning(
                f"'{selected}' is not in the current employee list. "
                "They may have been filtered out or removed from UKG for this period."
            )
            return

        # FRAGMENT — line editing stays scoped here, approve/unapprove
        # forces app-scoped rerun.
        _render_editor(period, labor_source, selected, reviewer_name, row_match.iloc[0], show_amounts)
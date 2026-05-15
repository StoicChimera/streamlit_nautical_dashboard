"""
wip_labor_review.py
===================

Review tab UI for per-employee allocation.

Drill-down layout:
  - Left column: employee list with search + filter
  - Right column: editor for the currently-selected employee

A "Multi-select for bulk approve" toggle in the employee list switches
the row interaction:

  - Single mode: one button per row (click to drill into the editor).
  - Multi  mode: a virtualized st.dataframe with native multi-row
    selection. The bulk-assign panel above the list reads the selection
    and applies a role + allocation template to every selected
    employee in one transaction.

The dataframe widget is virtualized — the browser paints only the
visible rows — so the multi-mode list scales to thousands of employees
at constant render cost. This replaces an earlier per-row checkbox
implementation that grew O(n) widget registrations and would freeze
the page above ~150 employees.

Because the bulk panel renders ABOVE the list visually but needs to
read the dataframe selection that the list produces, we reserve the
bulk panel's visual slot via st.empty() up front and fill it AFTER
the list has rendered (so the bulk panel sees the freshly synced
bulk_set_key on the same script run, with no one-click lag).

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


def _emp_dataframe_key(period, labor_source):
    """Widget key for the multi-mode employee selection dataframe.
    Exposed as a helper so the bulk-apply path can reset the widget
    state after a successful apply (otherwise stale selection rows
    re-populate bulk_set_key on the next render)."""
    return f"emp_dataframe_{period}_{labor_source}"


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
    """Compact single-line label for the single-mode row button."""
    name = str(row['employee_name'])
    cost = _dollar_or_hidden(row['total_labor_cost'], show_amounts)
    reviewed = bool(row['reviewed']) if pd.notna(row['reviewed']) else False
    is_new = bool(row.get('is_new_employee', False)) if 'is_new_employee' in row else False

    status = "Approved" if reviewed else "Pending"
    new_tag = " NEW" if is_new else ""
    return f"[{status}]{new_tag}  {name}  ·  {cost}"


# -------------------------------------------------------------
# Left column — employee list
# -------------------------------------------------------------

def _render_employee_list(period: str, labor_source: str, employees: pd.DataFrame, show_amounts: bool = False):
    """Renders the filterable employee list.

    Single mode: button per row (existing fast path, unchanged).
    Multi  mode: virtualized st.dataframe with native multi-row selection.
                 Selection is synced into bulk_set_key on every render
                 so the bulk panel (rendered AFTER this function via the
                 placeholder pattern in render_review_tab) sees fresh
                 state with no one-click lag.
    """
    multi_key    = _bulk_multi_key(period, labor_source)
    bulk_set_key = _bulk_set_key(period, labor_source)
    if bulk_set_key not in st.session_state:
        st.session_state[bulk_set_key] = set()

    sel_k = _selected_key(period, labor_source)
    currently_selected = st.session_state.get(sel_k)

    period_committed = wla.is_period_committed(period)

    # --- Mode toggle ---
    prior_multi = st.session_state.get(multi_key, False)
    multi = st.checkbox(
        "Multi-select for bulk approve",
        value=prior_multi,
        key=f"multi_toggle_widget_{period}_{labor_source}",
        help=(
            "Select multiple employees in the table below and apply the "
            "same role + allocation template to all at once via the bulk "
            "panel above the list."
        ),
        disabled=period_committed,
    )
    st.session_state[multi_key] = multi
    if multi != prior_multi:
        st.rerun()

    # --- Filters ---
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

    if df.empty:
        st.caption(f"Showing 0 of {len(employees)}")
        st.info("No employees match the current filter.")
        if multi and st.session_state.get(bulk_set_key):
            st.session_state[bulk_set_key] = set()
        return

    # =============================================================
    # MULTI MODE — st.multiselect for picking, st.dataframe for viewing.
    #
    # Earlier attempts used st.data_editor (Select checkbox column)
    # and then st.dataframe (selection_mode='multi-row'). Both lost
    # selection state across reruns — checkbox edits got dropped by
    # data_editor when display_df was rebuilt; dataframe selection
    # unhighlighted on rerun and offered no API to programmatically
    # restore it.
    #
    # st.multiselect doesn't have either problem. Its value is plain
    # session state, settable from outside the widget, with a search
    # box built in. The dataframe becomes a read-only reference for
    # cost/role context.
    # =============================================================
    if multi:
        is_reviewed = df['reviewed'].fillna(False) == True
        unreviewed = df[~is_reviewed].copy().reset_index(drop=True)
        n_approved_in_filter = int(is_reviewed.sum())

        prior_selected = st.session_state.get(bulk_set_key, set())

        if unreviewed.empty:
            st.caption(
                f"Showing 0 unreviewed of {len(employees)} total  "
                f"·  **{len(prior_selected)}** selected"
                + (f"  ·  {n_approved_in_filter} approved hidden" if n_approved_in_filter else "")
            )
            st.info(
                "No unreviewed employees match the current filter. "
                "Switch to single mode (uncheck the toggle above) to view "
                "and edit approved employees."
            )
            return

        # Build options as "Name [NEW]  —  UKG Role" so the dropdown
        # carries enough context to pick without cross-referencing.
        visible_names = list(unreviewed['employee_name'].astype(str))
        option_to_name: dict = {}
        options: list = []
        for _, row in unreviewed.iterrows():
            name = str(row['employee_name'])
            role = str(row.get('ukg_role', '') or '').strip()
            is_new = bool(row.get('is_new_employee', False))
            tag = ' [NEW]' if is_new else ''
            label = f"{name}{tag}  —  {role}" if role else f"{name}{tag}"
            options.append(label)
            option_to_name[label] = name

        ms_key = f"emp_multiselect_{period}_{labor_source}"

        # On filter change, re-seed the multiselect from bulk_set_key.
        # Anything in the batch that's also visible under the new
        # filter shows up as already-selected. Off-screen names stay
        # in bulk_set_key but don't render as chips.
        filter_sig = f"{search_term}|{filter_choice}"
        sig_key = f"emp_filter_sig_{period}_{labor_source}"
        if st.session_state.get(sig_key) != filter_sig:
            st.session_state[ms_key] = [
                label for label, name in option_to_name.items()
                if name in prior_selected
            ]
            st.session_state[sig_key] = filter_sig

        selected_options = st.multiselect(
            f"Select employees for bulk apply ({len(options)} visible)",
            options=options,
            key=ms_key,
            placeholder="Type to search, click to add. Click the X on a chip to remove.",
        )

        visible_selected = {option_to_name[l] for l in selected_options}
        visible_names_set = set(visible_names)
        preserved_offscreen = prior_selected - visible_names_set
        st.session_state[bulk_set_key] = preserved_offscreen | visible_selected

        n_after = len(st.session_state[bulk_set_key])

        cap = f"**{n_after}** in batch"
        if preserved_offscreen:
            cap += f"  ·  {len(preserved_offscreen)} off-screen retained"
        if n_approved_in_filter:
            cap += f"  ·  {n_approved_in_filter} approved hidden"
        st.caption(cap)

        # Action buttons
        not_yet_batched = visible_names_set - visible_selected
        col_select_all, col_clear = st.columns(2)

        with col_select_all:
            if not_yet_batched and st.button(
                f"Select all {len(not_yet_batched)} visible",
                key=f"select_all_visible_btn_{period}_{labor_source}",
                use_container_width=True,
                help=(
                    "Adds every name in the current filter view to the "
                    "multiselect. Off-screen names from prior filters "
                    "stay in the batch."
                ),
            ):
                st.session_state[ms_key] = options
                st.rerun()

        with col_clear:
            if n_after and st.button(
                f"Clear batch ({n_after})",
                key=f"clear_sel_btn_{period}_{labor_source}",
                use_container_width=True,
            ):
                st.session_state[ms_key] = []
                st.session_state[bulk_set_key] = set()
                st.rerun()

        # Reference table — read-only, no selection. Lets the user
        # see cost / type / role at a glance while picking from the
        # multiselect above.
        st.caption("Reference (read-only)")
        display_df = pd.DataFrame({
            'Employee': unreviewed['employee_name'].values,
            'Type':     unreviewed['is_new_employee']
                            .fillna(False)
                            .map({True: 'New hire', False: 'Returning'})
                            .values,
            'UKG Role': unreviewed.get('ukg_role', pd.Series([''] * len(unreviewed))).fillna('').values,
            'Cost':     [_dollar_or_hidden(v, show_amounts) for v in unreviewed['total_labor_cost'].values],
        })
        st.dataframe(
            display_df,
            use_container_width=True,
            height=300,
            hide_index=True,
            key=f"ref_table_{period}_{labor_source}",
        )

        return

    # =============================================================
    # SINGLE MODE — virtualized dataframe with single-row selection
    # Replaces the prior button-per-row pattern, which registered
    # 74+ widgets per render and made every interaction multi-minute
    # on Streamlit Cloud (per-widget websocket cost compounds).
    # =============================================================
    st.caption(f"Showing {len(df)} of {len(employees)}")

    # Sort: new hires pending → returning pending → approved (mirrors
    # the prior section grouping; user can re-sort by column header).
    df_sorted = df.copy()
    df_sorted['_status_order'] = (
        df_sorted['reviewed'].fillna(False).astype(int) * 2
        + (~df_sorted['is_new_employee'].fillna(False)).astype(int)
    )
    df_sorted = df_sorted.sort_values(
        ['_status_order', 'employee_name']
    ).reset_index(drop=True)

    display_df = pd.DataFrame({
        'Status': df_sorted['reviewed']
                      .fillna(False)
                      .map({True: 'Approved', False: 'Pending'})
                      .values,
        'Type': df_sorted['is_new_employee']
                    .fillna(False)
                    .map({True: 'New hire', False: 'Returning'})
                    .values,
        'Employee': df_sorted['employee_name'].values,
        'UKG Role': df_sorted.get('ukg_role', pd.Series([''] * len(df_sorted))).fillna('').values,
        'Cost': [
            _dollar_or_hidden(v, show_amounts)
            for v in df_sorted['total_labor_cost'].values
        ],
    })

    single_df_key = f"emp_single_df_{period}_{labor_source}"

    event = st.dataframe(
        display_df,
        use_container_width=True,
        height=520,
        hide_index=True,
        on_select='rerun',
        selection_mode='single-row',
        key=single_df_key,
        column_config={
            'Status':   st.column_config.TextColumn('Status', width='small'),
            'Type':     st.column_config.TextColumn('Type', width='small'),
            'Employee': st.column_config.TextColumn('Employee', width='medium'),
            'UKG Role': st.column_config.TextColumn('UKG Role'),
            'Cost':     st.column_config.TextColumn('Cost', width='small'),
        },
    )

    selected_idx: list = []
    if event is not None and getattr(event, 'selection', None) is not None:
        selected_idx = list(getattr(event.selection, 'rows', []) or [])

    if selected_idx:
        new_selected = df_sorted.iloc[selected_idx[0]]['employee_name']
        if new_selected != currently_selected:
            st.session_state[_selected_key(period, labor_source)] = new_selected
            st.rerun()


# -------------------------------------------------------------
# Bulk assign + approve panel
# -------------------------------------------------------------

def _render_bulk_assign_panel(
    period: str, labor_source: str,
    employees: pd.DataFrame, reviewer_name: str,
):
    """Bulk-apply a role + allocation template to many unreviewed employees.

    Two render states:
      - Multi-select OFF: collapsed expander with a hint.
      - Multi-select ON: full panel reading the selection from
        bulk_set_key (which the list populated from the dataframe
        widget on this same render via the placeholder pattern in
        render_review_tab).
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
                "below to start. Select the employees you want, define a role + "
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
                "No employees selected yet — define the template here, then "
                "select rows in the list below. The Apply button enables once "
                "you have a valid template + at least one employee."
            )
        else:
            preview = sorted(selected_set)[:5]
            preview_str = ", ".join(f"`{p}`" for p in preview)
            if n_selected > 5:
                preview_str += f" + {n_selected - 5} more"
            plural = "s" if n_selected != 1 else ""
            st.markdown(f"### Bulk assign and approve — {n_selected} employee{plural} selected")
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
                if st.button("Remove", key=rm_k, help="Remove this line"):
                    lines.pop(i)
                    st.session_state[lines_k] = lines
                    st.rerun()

        st.session_state[lines_k] = lines

        total = sum(float(ln.get('allocation_pct', 0)) for ln in lines)
        is_100 = abs(total - 1.0) < 1e-6

        col_add, col_total = st.columns([1, 3])
        with col_add:
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
            f"Apply to {n_selected} selected and approve"
            if n_selected > 0 else "Apply to selected and approve"
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
                # Clear the selection set AND the dataframe widget state
                # so the same template doesn't re-apply by accident on the
                # next batch. Leave multi mode + the role + lines template
                # alone so the user can keep batching with a tweaked role.
                st.session_state[bulk_set_key] = set()
                df_key = _emp_dataframe_key(period, labor_source)
                if df_key in st.session_state:
                    del st.session_state[df_key]
                st.rerun()
            except ValueError as e:
                st.error(str(e))


# -------------------------------------------------------------
# Right column — editor body
# -------------------------------------------------------------

def _render_editor(period: str, labor_source: str, employee: str, reviewer_name: str,
                   current_row: pd.Series, show_amounts: bool = False):
    """Renders the editor for the selected employee."""
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
            st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)
        else:
            st.caption("No allocation on file for this employee.")
        return

    _load_into_state(period, labor_source, employee)

    role_k   = _state_key(period, labor_source, employee, 'role')
    lines_k  = _state_key(period, labor_source, employee, 'lines')
    source_k = _state_key(period, labor_source, employee, 'carry_source')

    reviewed = bool(current_row['reviewed']) if pd.notna(current_row['reviewed']) else False
    status_badge = "Approved" if reviewed else "Not allocated"
    st.markdown(
            f"### {employee}  \n"
            f"{_dollar_or_hidden(current_row['total_labor_cost'], show_amounts)}  ·  {status_badge}"
        )

    if st.session_state.get(source_k):
        st.info(f"Carried forward from {st.session_state[source_k]} — review and approve.")

    ukg_prog = str(current_row.get('ukg_program') or '')
    ukg_role = str(current_row.get('ukg_role') or '')
    if ukg_prog or ukg_role:
        st.caption(f"UKG context — program: {ukg_prog}  ·  role: {ukg_role}")

    st.markdown("")

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
            if st.button("Remove", key=rm_k, help="Remove this line"):
                lines.pop(i)
                st.session_state[lines_k] = lines
                st.rerun()

    st.session_state[lines_k] = lines

    total = sum(float(ln.get('allocation_pct', 0)) for ln in lines)
    is_100 = abs(total - 1.0) < 1e-6

    col_add, col_total = st.columns([1, 3])
    with col_add:
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
                    st.rerun()
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
            st.rerun()


# -------------------------------------------------------------
# Public entry point
# -------------------------------------------------------------

def render_review_tab(period: str, labor_source: str, reviewer_name: str, show_amounts: bool = False):
    """Main review tab. labor_source is 'direct' or 'temp'.

    Layout order:
      1. KPIs (top-level)
      2. Carry-forward + approve quick button
      3. Bulk panel slot (RESERVED via st.empty, filled later)
      4. List + Editor (two-column drill-down)
      5. Bulk panel content actually rendered into the slot from step 3

    Why the placeholder: in multi mode the bulk panel needs to read
    bulk_set_key AFTER the list has synced it from the dataframe
    selection. Reserving the visual slot up front lets us render the
    bulk panel content last while still showing it above the list.
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

    # --- Carry-forward + approve quick button ---
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

    # --- Reserve the bulk panel slot. Filled AFTER the list so the
    # bulk panel reads fresh bulk_set_key on the same script run.
    bulk_panel_slot = st.empty()

    st.markdown("")

    # --- List + Editor ---
    col_list, col_editor = st.columns([2, 3], gap="medium")

    with col_list:
        _render_employee_list(period, labor_source, employees, show_amounts)

    with col_editor:
        multi_key = _bulk_multi_key(period, labor_source)
        if st.session_state.get(multi_key, False):
            st.info(
                "Multi-select mode is on. Use the **Bulk assign and approve** "
                "panel above to apply a role + allocation template to all "
                "selected employees at once. Switch off **Multi-select for "
                "bulk approve** in the employee list to drill into a single "
                "employee."
            )
        else:
            sel_k = _selected_key(period, labor_source)
            selected = st.session_state.get(sel_k)

            if not selected:
                st.info("← Pick an employee from the list to start allocating.")
            else:
                row_match = employees[employees['employee_name'] == selected]
                if row_match.empty:
                    st.warning(
                        f"'{selected}' is not in the current employee list. "
                        "They may have been filtered out or removed from UKG for this period."
                    )
                else:
                    _render_editor(period, labor_source, selected, reviewer_name, row_match.iloc[0], show_amounts)

    # --- Render bulk panel into the reserved slot. By now, the list
    # has run and bulk_set_key reflects the dataframe selection.
    with bulk_panel_slot.container():
        _render_bulk_assign_panel(period, labor_source, employees, reviewer_name)
    
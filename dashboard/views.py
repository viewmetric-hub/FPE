from django.shortcuts import render


def logout_view(request):
    return render(request, 'dashboard/logout.html')


def login_page(request):
    return render(request, 'dashboard/login.html')


def dashboard_page(request):
    return render(request, 'dashboard/dashboard_router.html')


def platform_admin_dashboard_page(request):
    return render(request, 'dashboard/dashboard_router.html')


def consumer_manager_dashboard_page(request):
    return render(
        request,
        'dashboard/consumer_manager_dashboard_kaiadmin.html',
        {'kai_active_nav': 'dashboard'},
    )


def plant_user_dashboard_page(request):
    return render(
        request,
        'dashboard/plant_user_dashboard_kaiadmin.html',
        {'kai_active_nav': 'dashboard'},
    )


def demand_entry_page(request):
    # Backward compatible route - render the list view.
    return render(
        request,
        'dashboard/demand_entry_list.html',
        {'kai_active_nav': 'demand_entry'},
    )


def demand_entry_list_page(request):
    return render(
        request,
        'dashboard/demand_entry_list.html',
        {'kai_active_nav': 'demand_entry'},
    )


def demand_entry_schedule_page(request, date_str: str):
    # date_str must be in YYYY-MM-DD format; frontend will call the API with this value.
    return render(
        request,
        'dashboard/demand_entry_schedule.html',
        {'date_str': date_str, 'kai_active_nav': 'demand_entry'},
    )


def plant_user_allocations_page(request):
    return render(
        request,
        'dashboard/plant_user_allocations_kaiadmin.html',
        {'kai_active_nav': 'allocations'},
    )


def consumer_manager_demand_entry_page(request):
    return render(
        request,
        'dashboard/consumer_manager_demand_entry.html',
        {'kai_active_nav': 'demand_entry'},
    )


def generator_dashboard_page(request):
    return render(
        request,
        'dashboard/generator_dashboard_kaiadmin.html',
        {'kai_active_nav': 'dashboard1'},
    )


def generator_demand_page(request):
    return render(
        request,
        'dashboard/generator_demand.html',
        {'kai_active_nav': 'demand'},
    )


def generator_allocation_page(request):
    return render(
        request,
        'dashboard/generator_allocation.html',
        {'kai_active_nav': 'allocation'},
    )


def generator_schedule_revisions_page(request):
    return render(
        request,
        'dashboard/generator_schedule_revisions.html',
        {'kai_active_nav': 'schedule_revisions'},
    )


def generator_allocation_edit_page(request, consumer_manager_user_id: int, date_str: str):
    return render(
        request,
        'dashboard/generator_allocation_edit.html',
        {
            'consumer_manager_user_id': consumer_manager_user_id,
            'date_str': date_str,
            'kai_active_nav': 'allocation',
            'schedule_view_only': request.GET.get('mode') == 'view',
        },
    )


def consumer_allocation_page(request):
    return render(
        request,
        'dashboard/consumer_manager_allocation.html',
        {'kai_active_nav': 'allocation'},
    )


def consumer_plantwise_allocation_page(request):
    return render(
        request,
        'dashboard/consumer_plantwise_allocation.html',
        {'kai_active_nav': 'plantwise'},
    )


def plant_management_page(request):
    return render(
        request,
        'dashboard/consumer_manager_plant_management_kaiadmin.html',
        {'kai_active_nav': 'plant_mgmt'},
    )


def iex_predictor_page(request):
    return render(
        request,
        'dashboard/iex_predictor.html',
        {'kai_active_nav': 'iex'},
    )


def reports_page(request):
    """
    Consumer Manager / Platform Admin reports (JWT auth does not populate Django session,
    so do not rely on request.user.role here). Plant users should use plant_user_reports_page
    at /plant-user-reports/; see also client-side redirect in reports.html for bookmarks.
    """
    return render(
        request,
        'dashboard/reports.html',
        {'kai_active_nav': 'reports'},
    )


def plant_user_reports_page(request):
    """Plant-scoped reports UI (same partials as /reports/ but plant_user base layout)."""
    return render(
        request,
        'dashboard/plant_user_reports_kaiadmin.html',
        {'kai_active_nav': 'reports'},
    )


def generator_reports_page(request):
    return render(
        request,
        'dashboard/generator_reports.html',
        {'kai_active_nav': 'reports'},
    )


def settings_view(request):
    """Consumer manager / shared settings shell. Plant users use JWT-only auth, so `request.user` is
    often anonymous here; they are redirected client-side to `plant_user_settings_page`."""
    return render(
        request,
        'dashboard/settings.html',
        {'kai_active_nav': 'settings'},
    )


def plant_user_settings_page(request):
    """Settings UI with plant-user sidebar (same as other plant Kaiadmin pages)."""
    return render(
        request,
        'dashboard/plant_user_settings_kaiadmin.html',
        {'kai_active_nav': 'settings'},
    )


def generator_settings_page(request):
    return render(
        request,
        'dashboard/settings_kaiadmin.html',
        {'kai_active_nav': 'settings'},
    )

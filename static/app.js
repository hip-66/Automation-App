// PS Automation - Frontend Controller
document.addEventListener("DOMContentLoaded", () => {

    // =====================================================================
    // Persistent user preferences
    // =====================================================================
    const PREFS_KEY = "ps_app_prefs";

    function loadPrefs() {
        try {
            return JSON.parse(localStorage.getItem(PREFS_KEY)) || {};
        } catch (e) {
            return {};
        }
    }

    function savePref(key, value) {
        const prefs = loadPrefs();
        prefs[key] = value;
        localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    }

    let prefs = loadPrefs();

    let currentLanguage = prefs.lang || localStorage.getItem("app_lang") || "he";
    let selectedScript = null;
    let scriptsData = [];
    let activeRunId = null;
    let eventSource = null;
    let statusPollTimer = null;
    let historyPollTimer = null;
    let runStarting = false;
    // Live job window (Jenkins-style) state - declared up here so the very first
    // translateUI()/renderJobTray() on load can't hit a temporal-dead-zone.
    let trayJob = null;   // {runId,name,status,servers,targets,total,startTs,endTs,finalStatus}
    // Cumulative time-saved stats - declared up here for the same reason
    // (translateUI() -> renderTimeSaved() runs during initial load).
    let timeSaved = null;   // {seconds, minutes, runs, servers}
    let dashAnalytics = null;   // /api/stats/analytics payload for the dashboard charts
    let dashRangeDays = 14;     // runs-over-time window
    let dashRangeBound = false; // range filter listener attached once
    // Per-server console filter: null = show the full merged log; an IP = show
    // only that server's session lines. Driven by clicking a server chip.
    let activeServerFilter = null;
    let currentPage = (location.hash || "").replace("#", "") || "dashboard";
    if (!["dashboard", "wizard", "targets", "scheduling", "reports", "logs"].includes(currentPage)) currentPage = "dashboard";
    let currentCommandProfile = prefs.last_profile || "atp";
    let environmentInfo = {};
    const ENV_CACHE_KEY = "psauto_env_cache_v1";
    let commandProfiles = {};
    let historyData = [];
    let lastRunSummary = null;
    let companiesData = {};
    let ctx = { company: "", language: "", stage: "" };
    let defaultCreds = { idrac_username: "root", idrac_password: "", ssh_username: "root", ssh_password: "" };
    let allScriptsData = [];      // unscoped (all companies) - used for risk breakdown + audit lookups
    // Checked-row state for the Reports/Logs delete checkboxes, kept OUTSIDE
    // the table markup itself: the table re-renders wholesale every few
    // seconds (history polling / manual refresh), which would otherwise wipe
    // out whatever the user had just checked.
    let checkedReportPaths = new Set();
    let checkedLogRunIds = new Set();
    let targetGroupsData = [];
    let WIZ = { step: 1, finished: true, pendingTargetGroupId: null };
    // Consumed once by enterWizardPage() to mark the next fresh Wizard entry
    // as "configuring a schedule" (set by the Scheduling page's "+ New" button).
    let wizSchedulingIntent = false;
    let schedulesData = [];

    // =====================================================================
    // Theme (Dark / Light) - persisted, applied via data-theme on <html>
    // =====================================================================
    const THEME_KEY = "app_theme";
    let currentTheme = localStorage.getItem(THEME_KEY) || "dark";

    // Labeled theme button: shows the CURRENT mode ("🌙 כהה" / "☀️ בהיר",
    // or "🌙 Dark" / "☀️ Light"). Guarded t() so it also works at init,
    // before the translations object is initialized.
    function updateThemeButtons() {
        const isDark = currentTheme === "dark";
        let dark = "כהה", light = "בהיר";
        try { const tr = t(); if (tr) { dark = tr.theme_dark || dark; light = tr.theme_light || light; } } catch (e) {}
        document.querySelectorAll("#theme-toggle").forEach(btn => {
            btn.textContent = isDark ? ("🌙 " + dark) : ("☀️ " + light);
        });
    }

    function applyTheme(theme) {
        currentTheme = theme === "dark" ? "dark" : "light";
        document.documentElement.setAttribute("data-theme", currentTheme);
        localStorage.setItem(THEME_KEY, currentTheme);
        updateThemeButtons();
    }

    document.querySelectorAll("#theme-toggle").forEach(btn => {
        btn.addEventListener("click", () => applyTheme(currentTheme === "dark" ? "light" : "dark"));
    });
    applyTheme(currentTheme);

    // ---------------------------------------------------------------------
    // Logout
    // ---------------------------------------------------------------------
    function performLogout() {
        try { sessionStorage.removeItem(ENV_CACHE_KEY); } catch (e) { /* ignore */ }
        // Fire the server-side logout WITHOUT waiting on it, then navigate
        // immediately - the redirect must never hang on a slow/failed fetch
        // (that was why an idle logout could leave the app on screen until a
        // manual refresh).
        try { if (navigator.sendBeacon) navigator.sendBeacon("/api/logout"); } catch (e) { /* ignore */ }
        try { fetch("/api/logout", { method: "POST", keepalive: true }).catch(() => {}); } catch (e) { /* ignore */ }
        window.location.href = "/login";
    }

    const logoutBtn = document.getElementById("logout-btn");
    if (logoutBtn) {
        logoutBtn.addEventListener("click", () => { performLogout(); });
    }
    function updateLogoutButton() {
        if (!logoutBtn) return;
        let label = "🚪 התנתקות";
        try { const tr = t(); if (tr && tr.logout_label) label = "🚪 " + tr.logout_label; } catch (e) {}
        logoutBtn.textContent = label;
    }
    updateLogoutButton();

    // ---------------------------------------------------------------------
    // Global session-expiry guard: the server can invalidate every open
    // session without any client-side idle timeout ever firing (e.g. the app
    // process restarts, which deliberately regenerates the session-signing
    // key on every launch - see server.py). Without this, every API call
    // made afterwards just gets a 401 that nothing looks at, so the app
    // keeps looking normal while nothing actually works until a manual
    // refresh. Catch it the moment ANY fetch() reveals the session is dead.
    // ---------------------------------------------------------------------
    let sessionExpiredHandled = false;
    const _origFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
        return _origFetch(input, init).then((response) => {
            const url = typeof input === "string" ? input : (input && input.url) || "";
            if (response.status === 401 && !url.includes("/api/login") && !sessionExpiredHandled) {
                sessionExpiredHandled = true;
                performLogout();
            }
            return response;
        });
    };

    // ---------------------------------------------------------------------
    // Idle auto-logout: 15 minutes with no real mouse/keyboard/touch
    // activity sends you back to the login page, so an unattended session
    // can't be used by anyone who isn't logged in themselves. Background
    // polling (script list refresh, history refresh, etc.) does NOT count
    // as activity - only genuine user interaction resets the timer.
    // ---------------------------------------------------------------------
    const IDLE_TIMEOUT_MS = 15 * 60 * 1000;
    const IDLE_WARNING_MS = 60 * 1000; // show a warning toast this long before the auto-logout fires
    let lastActivityAt = Date.now();
    let idleWarningShown = false;

    function hideIdleWarning() {
        idleWarningShown = false;
        const el = document.getElementById("idle-warning-toast");
        if (el) el.style.display = "none";
    }

    function showIdleWarning() {
        idleWarningShown = true;
        const tr = t();
        const el = document.getElementById("idle-warning-toast");
        if (!el) return;
        document.getElementById("idle-warning-title").textContent = tr.idle_warning_title;
        document.getElementById("idle-warning-body").textContent = tr.idle_warning_body;
        document.getElementById("idle-warning-stay-btn").textContent = tr.idle_warning_stay_btn;
        el.style.display = "flex";
    }

    // An automation in flight counts as activity: NEVER auto-logout while a
    // script is running (the run always finishes and stays monitored).
    function automationActive() { return !!(activeRunId || runStarting); }
    function idleExpired() { return (Date.now() - lastActivityAt) >= IDLE_TIMEOUT_MS; }

    ["mousemove", "mousedown", "keydown", "scroll", "touchstart", "click"].forEach(evt => {
        window.addEventListener(evt, () => {
            // Returning after 15+ idle minutes: the FIRST interaction logs out
            // (unless a run is active) instead of silently resetting the idle
            // clock - previously that reset raced ahead of the 5s checker, so
            // the logout never fired and the app stayed open until a refresh.
            if (idleExpired() && !automationActive()) { performLogout(); return; }
            lastActivityAt = Date.now();
            if (idleWarningShown) hideIdleWarning();
        }, { passive: true });
    });

    // Also catch the moment the tab becomes visible again (browser timers are
    // heavily throttled in background tabs, so the interval below may not have
    // fired while the tab was hidden).
    document.addEventListener("visibilitychange", () => {
        if (!document.hidden && idleExpired() && !automationActive()) performLogout();
    });

    const idleWarningStayBtn = document.getElementById("idle-warning-stay-btn");
    if (idleWarningStayBtn) {
        idleWarningStayBtn.addEventListener("click", () => {
            lastActivityAt = Date.now();
            hideIdleWarning();
        });
    }

    setInterval(() => {
        // A running automation keeps the session alive: the idle clock only
        // starts counting once nothing is running.
        if (automationActive()) { lastActivityAt = Date.now(); return; }
        const idleFor = Date.now() - lastActivityAt;
        if (idleFor >= IDLE_TIMEOUT_MS) {
            performLogout();
        } else if (idleFor >= IDLE_TIMEOUT_MS - IDLE_WARNING_MS && !idleWarningShown) {
            showIdleWarning();
        }
    }, 5000);

    // =====================================================================
    // Translations dictionary
    // =====================================================================
    const TRANSLATIONS = {
        he: {
            brand_title: "PS Automation",
            sidebar_scripts: "אוטומציות זמינות",
            chromedriver_label: "גרסת Chromedriver:",
            lang_label: "שפה / Language:",
            theme_label: "מצב תצוגה",
            theme_dark: "כהה",
            theme_light: "בהיר",
            logout_label: "התנתקות",
            settings_btn: "הגדרות",
            idle_warning_title: "תנותק בקרוב",
            idle_warning_body: "עקב חוסר פעילות תנותק תוך דקה. לחץ כדי להישאר מחובר.",
            idle_warning_stay_btn: "הישאר מחובר",
            default_header_title: "בחר אוטומציה להרצה",
            default_header_desc: "בחר אחת מהאוטומציות בתפריט כדי להתחיל בהגדרה והרצה.",
            status_running: "הרצה פעילה...",
            status_finished: "הרצה הושלמה בהצלחה",
            status_failed: "הרצה נכשלה",
            status_killed: "הרצה הופסקה על ידי המשתמש",
            form_title: "פרמטרים והגדרות הרצה",
            form_placeholder: "אנא בחר אוטומציה מתפריט הצד על מנת להציג את אפשרויות ההגדרה.",
            form_no_inputs: "אוטומציה זו רצה באופן מקומי וללא צורך בפרמטרים נוספים. לחץ 'הרץ' כדי להתחיל.",
            btn_run: "הרץ אוטומציה פעילה",
            btn_kill: "עצור הרצה באופן מיידי",
            console_title: "פלט הרצה בזמן אמת (Console)",
            console_welcome: "> מערכת בקרת הרצה מוכנה. פלט סקריפטים יופיע כאן בזמן אמת...",
            clear_console_tip: "נקה קונסולה",
            nav_dashboard: "דשבורד",
            nav_logs: "לוגים",
            nav_files: "פלטים",
            logs_page_title: "לוגים - היסטוריית הרצות",
            files_page_title: "פלטים - קבצים שנוצרו",
            btn_open_logs_folder: "פתח תיקיית Logs",
            btn_open_output_folder: "פתח תיקיית פלטים",
            btn_goto_logs: "עבור ללוגים",
            chrome_field_label: "גרסת כרום (ChromeDriver):",
            edit_desc_tip: "ערוך תיאור",
            edit_desc_modal_title: "עריכת תיאור",
            edit_desc_he_label: "תיאור בעברית",
            edit_desc_en_label: "תיאור באנגלית",
            toast_desc_saved: "התיאור נשמר בהצלחה",
            btn_open_run_log: "פתח לוג הרצה",
            tab_history: "היסטוריית הרצות",
            tab_files: "קבצי דוחות",
            btn_open_reports: "תיקיית תוצאות",
            btn_open_report_folder: "Report Folder",
            btn_refresh: "רענן",
            btn_export: "ייצוא ל-Excel",
            open_scripts_folder: "פתח תיקיית סקריפטים",
            placeholder_history: "אין היסטוריית ריצה זמינה.",
            placeholder_files: "אין דוחות או לוגים זמינים בתיקייה.",
            th_automation: "אוטומציה",
            th_date: "תאריך",
            th_start: "התחלה",
            th_duration: "משך",
            th_user: "משתמש",
            th_machine_ip: "IP מכונה",
            th_servers: "שרתים",
            th_status: "סטטוס",
            th_result: "קובץ לוג",
            th_fail_reason: "סיבת כישלון",
            th_actions: "פעולות",
            th_file_name: "שם הקובץ",
            th_modified: "תאריך יצירה",
            th_size: "גודל",
            btn_open: "פתח",
            btn_reveal: "הצג בתיקייה",
            btn_delete: "מחק",
            reports_delete_selected: "מחק נבחרים",
            logs_delete_selected: "מחק נבחרים",
            delete_confirm_title: "אישור מחיקה",
            delete_report_confirm_body: "האם אתה בטוח שברצונך למחוק {n} דוחות? פעולה זו אינה הפיכה.",
            delete_log_confirm_body: "האם אתה בטוח שברצונך למחוק {n} רשומות לוג? פעולה זו אינה הפיכה.",
            delete_partial_failed: "חלק מהקבצים לא נמחקו",
            toast_deleted: "נמחק בהצלחה",
            btn_details: "פרטים",
            btn_run_again: "הרץ שוב",
            status_completed_badge: "הושלם",
            status_failed_badge: "נכשל",
            status_killed_badge: "הופסק",
            status_running_badge: "רץ...",
            toast_start: "מפעיל את האוטומציה...",
            toast_success: "ההרצה הסתיימה בהצלחה!",
            toast_failed: "שגיאה! ההרצה נכשלה.",
            toast_killed: "ההרצה הופסקה באופן יזום.",
            toast_reports_refreshed: "רשימת הדוחות עודכנה",
            toast_console_cleared: "הקונסולה נוקתה",
            toast_copied: "הסיכום הועתק ללוח",
            toast_profile_saved: "פרופיל הפקודות נשמר",
            toast_file_opened: "הקובץ נפתח בהצלחה",
            toast_run_again_loaded: "הטופס מולא מהרצה קודמת - ערוך במידת הצורך ולחץ הרץ",
            mode_label: "שיטת הזנת שרתים (IP Mode):",
            mode_range: "טווח כתובות (Sequence)",
            mode_list: "רשימת כתובות ידנית (Custom List)",
            base_ip: "כתובת IP בסיסית - 3 החלקים הראשונים בלבד (לדוגמה: 192.168.0):",
            start_suffix: "סיומת התחלתית (לדוגמה: 207):",
            count: "מספר שרתים להרצה:",
            ips_list: "רשימת כתובות IP (כתובת אחת בשורה):",
            ips_list_current: "כתובות iDRAC נוכחיות (כתובת אחת בשורה - אליהן מתחברים):",
            ips_range_hint: "אפשר גם לכתוב טווח בשורה אחת, לדוגמה: 192.168.0.1-192.168.0.3 (במקום 3 שורות נפרדות)",
            hostnames_list: "רשימת שמות (Hostname) - שם אחד בשורה, השם הראשון שייך לכתובת הראשונה וכן הלאה:",
            hostnames_range_hint: "אפשר גם לכתוב טווח בשורה אחת, לדוגמה: kafka1-kafka3 (במקום 3 שורות נפרדות)",
            newips_list: "iDRAC IP by NDD - כתובות iDRAC חדשות (שורה אחת לכל שרת, הכתובת הראשונה שייכת ל-iDRAC הראשון וכן הלאה):",
            newips_range_hint: "אפשר גם לכתוב טווח בשורה אחת, לדוגמה: 192.168.0.101-192.168.0.103 (במקום 3 שורות נפרדות)",
            netmask_label: "Netmask (אחיד לכל השרתים - השאר ריק כדי לא לשנות):",
            netmask_hint: "לדוגמה: 255.255.255.0",
            gateway_label: "Gateway (אחיד לכל השרתים - השאר ריק כדי לא לשנות):",
            gateway_hint: "לדוגמה: 10.201.91.254",
            raid_name_label: "שם ה-RAID (Virtual Disk) שייווצר:",
            raid_name_hint: "ברירת מחדל: vDisk1. אפשר לשנות. חובה למלא שם - RAID לא יכול להיווצר בלי שם.",
            raid_name_required: "חובה להזין שם ל-RAID (RAID לא יכול להיווצר בלי שם).",
            jt_servers: "שרתים",
            jt_state_pending: "בתור",
            jt_state_running: "רץ",
            jt_state_success: "הצליח",
            jt_state_failed: "נכשל",
            jt_stop: "עצור משימה",
            jt_close: "סגור חלונית",
            jt_open_live: "פתח את מסך ההרצה החי",
            ts_title: "⏱ זמן שנחסך",
            ts_lead: "מצטבר · כל הזמנים",
            ts_unit_min: "דקות",
            ts_unit_hour: "שעות",
            ts_unit_day: "ימים",
            dns_list: "שרתי DNS להוספה (אחד בשורה) - ייכנסו לראש הרשימה ב-resolv.conf:",
            ntp_list: "שרתי NTP להוספה (אחד בשורה) - ייכנסו לראש הרשימה ב-chrony.conf (שורות pool יימחקו):",
            username: "שם משתמש:",
            password: "סיסמה:",
            password_show_tip: "הצג סיסמה",
            password_hide_tip: "הסתר סיסמה",
            use_default_creds: "השתמש בשם משתמש וסיסמה ברירת מחדל",
            yes_y: "כן (y)",
            no_n: "לא (n)",
            commands_label: "פקודות להרצה בשרת:",
            server_type_label: "בחירת פרופיל פקודות (Server Type):",
            custom_commands: "מותאם אישית (Custom)",
            save_profile_btn: "+ שמור כפרופיל חדש",
            save_profile_prompt: "הזן שם לפרופיל השרת החדש (למשל: MONGO):",
            delete_profile_confirm: "האם למחוק את הפרופיל",
            confirm_run_title: "אישור הרצה",
            confirm_run_body: "האם להפעיל את האוטומציה:",
            confirm_run_servers: "מספר שרתי יעד:",
            confirm_driver_warning: "אזהרה: גרסת ה-ChromeDriver שנבחרה אינה תואמת לגרסת ה-Chrome המותקנת!",
            confirm_kill_title: "עצירת הרצה",
            confirm_kill_body: "האם לעצור את ההרצה הפעילה באופן מיידי? פעולה זו תסגור את כל התהליכים הפעילים של ההרצה.",
            btn_confirm: "אישור והפעלה",
            btn_confirm_kill: "כן, עצור עכשיו",
            btn_cancel: "ביטול",
            btn_save: "שמור",
            translate_working: "מתרגם אוטומטית...",
            translate_offline: "תרגום אוטומטי לא זמין (אין אינטרנט) - מלא ידנית",
            progress_of: "מתוך",
            stage_waiting: "ממתין לתחילת עיבוד...",
            chip_all_servers: "הכל (מאוחד)",
            summary_title: "סיכום הרצה",
            summary_success_banner: "ההרצה הסתיימה בהצלחה",
            summary_failed_banner: "ההרצה נכשלה - בדוק את הפירוט למטה",
            summary_killed_banner: "ההרצה הופסקה על ידי המשתמש",
            summary_what_ran: "מה הורץ",
            summary_started: "שעת התחלה",
            summary_ended: "שעת סיום",
            summary_duration: "משך כולל",
            summary_user: "הופעל על ידי",
            summary_computer: "מחשב מפעיל",
            summary_servers_section: "שרתים שנבדקו",
            summary_success_actions: "פעולות שהצליחו",
            summary_failed_actions: "פעולות שנכשלו",
            summary_reports_section: "דוחות שנוצרו ומיקומם",
            summary_no_servers: "לא דווחו שרתים ספציפיים בהרצה זו.",
            summary_no_reports: "לא נוצרו קבצי דוחות בהרצה זו.",
            summary_none: "אין",
            btn_copy_summary: "העתק סיכום",
            btn_open_all_reports: "פתח את תיקיית הפלט",
            toast_output_revealed: "תיקיית הפלט נפתחה — הפלט של ההרצה מסומן",
            details_title: "פרטי הרצה",
            details_end: "שעת סיום",
            details_computer: "מחשב",
            details_servers: "שרתי יעד",
            details_outputs: "קבצים שנוצרו",
            filter_date: "סינון לפי תאריך",
            filter_type_all: "כל סוגי התהליכים",
            filter_server: "סינון לפי שרת...",
            filter_machine_ip: "סינון לפי IP מכונה...",
            filter_status_all: "כל הסטטוסים",
            filter_search: "חיפוש חופשי...",
            filter_clear: "נקה סינון",
            chrome_detected: "Chrome מותקן:",
            chrome_not_detected: "לא זוהתה התקנת Google Chrome",
            driver_recommended: "(מומלץ - תואם ל-Chrome)",
            driver_no_match: "אין ChromeDriver תואם לגרסת ה-Chrome המותקנת! הוסף גרסה מתאימה לתיקיית Chromedrivers (אין צורך בשינוי קוד).",
            error_prefix: "שגיאה",
            error_details: "פרטים",
            error_connection: "שגיאת תקשורת מול השרת המקומי. ודא ש-server.py פעיל.",
            run_btn_tip: "מפעיל את האוטומציה הנבחרת עם הפרמטרים שהוגדרו",
            kill_btn_tip: "עוצר מיידית את ההרצה הפעילה ואת כל תהליכי המשנה",
            version_label: "גרסה",
            entry_company_label: "חברה",
            entry_stage_label: "שלב",
            entry_add_stage_btn: "+ הוסף שלב",
            entry_add_stage_prompt: "שם השלב החדש (למשל: Firewall):",
            add_automation_btn: "הוסף אוטומציה",
            add_automation_modal_title: "הוספת אוטומציה חדשה",
            add_automation_intro: "בחר יעד בעץ התיקיות, ואז בחר קובץ סקריפט להעברה אליו.",
            add_automation_tree_label: "יעד בעץ התיקיות",
            add_automation_name_label: "שם תצוגה (אופציונלי)",
            add_automation_desc_he_label: "תיאור בעברית (אופציונלי)",
            add_automation_desc_en_label: "תיאור באנגלית (אופציונלי)",
            add_automation_file_label: "קובץ סקריפט להעברה",
            add_automation_no_files: "לא נמצאו קבצים לא-משויכים מהשפה הזו. הוסף קובץ .py/.ps1 ישירות לתיקיית Scripts ונסה שוב.",
            add_automation_no_dest: "בחר יעד בעץ התיקיות משמאל",
            add_automation_save_btn: "העבר והוסף",
            no_automations_placeholder: "אין עדיין אוטומציות עבור הבחירה הנוכחית. לחץ על 'הוסף אוטומציה' כדי להוסיף.",
            toast_stage_added: "השלב נוסף בהצלחה",
            toast_automation_added: "האוטומציה הועברה ונוספה בהצלחה",
            run_again_confirm_title: "הרצה חוזרת",
            run_again_confirm_body: "הפרמטרים של ההרצה הקודמת יוטענו לטופס ההרצה. תוכל לערוך אותם לפני שתלחץ הרץ. להמשיך?",
            run_again_unavailable: "לא ניתן לטעון הרצה זו מחדש (מידע חסר או שהאוטומציה הוסרה).",

            // --- New shell: nav groups / pages (Dashboard/Wizard/Targets/Reports/Logs/Audit/Settings) ---
            nav_group_main: "ראשי",
            nav_group_results: "תוצאות",
            nav_group_admin: "ניהול",
            nav_wizard: "הרצת אוטומציה",
            nav_targets: "קבוצות שרתים",
            nav_reports: "דוחות",

            risk_read: "קריאה בלבד",
            risk_config: "הגדרה",
            risk_destructive: "הרסני",
            wiz_cat_title: "קטגוריות",
            cat_all: "כל האוטומציות",
            cat_minutes: "דקות",
            cat_seconds: "שניות",
            categories: {
                report: "דוחות",
                configuration: "הגדרה",
                power: "כיבוי / הפעלה",
                storage: "אחסון / RAID",
                network: "רשת",
                validation: "בדיקות ואימות",
                redhat_validation: "אימות RedHat",
                windows: "Windows",
                general: "כללי"
            },
            dash_risk_count_suffix: "אוטומציות",
            dash_risk_caption: "קטגוריות הסיכון קובעות את רמת האישור לפני ההרצה.",

            dash_title: "דשבורד",
            dash_subtitle: "מבט-על על מצב המערכת וההרצות",
            dash_new_run: "הרצה חדשה",
            dash_env_title: "בריאות הסביבה",
            dash_env_disk: "מקום פנוי בדיסק",
            dash_risk_title: "אוטומציות לפי סיכון",
            dash_quick_title: "פעולות מהירות",
            dash_recent_title: "הרצות אחרונות",
            dash_recent_all: "הצג הכל",
            dash_col_risk: "סיכון",
            dash_stat_runs_today: "הרצות היום",
            dash_stat_success_rate: "אחוז הצלחה",
            dash_stat_succeeded: "הצליחו",
            dash_stat_screenshots: "צילומי מסך שנלכדו אוטומטית",
            dash_stat_screenshots_sub: 'סה"כ מצטבר (לא יורד עם מחיקת דוחות)',
            ss_clear_btn: "נקה",
            ss_clear_title: "איפוס מונה צילומי המסך",
            ss_clear_prompt: "הזן סיסמה כדי לאפס את המונה לאפס:",
            ss_clear_placeholder: "סיסמה",
            ss_clear_wrong: "סיסמה שגויה - המונה לא אופס",
            ss_clear_done: "מונה צילומי המסך אופס",
            dash_stat_running: "רצות כעת",

            wiz_title: "הרצת אוטומציה",
            wiz_subtitle: "אשף מודרך עם בדיקות לפני הרצה",
            wiz_step1_label: "בחירת אוטומציה",
            wiz_step2_label: "יעדים",
            wiz_step3_label: "בדיקות (Pre-flight)",
            wiz_step4_label: "אישור והרצה",
            wiz_search_label: "חיפוש אוטומציה",
            wiz_pick_title: "בחר אוטומציה להרצה",
            wiz_btn_continue: "המשך",
            wiz_targets_for: "יעדים עבור",
            wiz_target_source_label: "מקור יעדים",
            wiz_src_manual: "הזנה ידנית",
            wiz_src_group: "קבוצה שמורה",
            wiz_btn_back: "חזרה",
            wiz_btn_continue_preflight: "המשך לבדיקות",
            wiz_preflight_title: "בדיקות לפני הרצה (Pre-flight)",
            wiz_checking: "בודק...",
            wiz_pending: "ממתין",
            wiz_check_addr: "אימות ונרמול כתובות",
            wiz_check_reach: "בדיקת זמינות (ping)",
            wiz_check_env: "בדיקת סביבה",
            wiz_addr_valid: "כתובות תקינות",
            wiz_addr_dupes: "כפילויות",
            wiz_addr_invalid: "לא תקינות",
            wiz_addr_none: "לא הוזנו כתובות",
            wiz_reach_ok: "זמינים",
            wiz_reach_warn: "שרתים לא הגיבו לפינג - הם עדיין ייכללו בהרצה, אך ייתכן שייכשלו",
            wiz_reach_error: "בדיקת הזמינות נכשלה (תמשיך בזהירות)",
            wiz_env_ok: "תקין",
            wiz_risk_all: "כל רמות הסיכון",
            wiz_btn_continue_confirm: "המשך לאישור",
            wiz_confirm_title: "סיכום ואישור",
            wiz_live_label: "מריץ כעת:",
            wiz_more: "נוספים",
            wiz_destructive_warning: "פעולה הרסנית! פעולה זו אינה הפיכה.",
            wiz_type_confirm: "הקלד CONFIRM כדי לאשר את ההרצה.",
            wiz_confirm_placeholder: "הקלד CONFIRM",
            wiz_btn_run: "הרץ אוטומציה",
            wiz_live_total: 'סה"כ שרתים',
            wiz_live_done: "הושלמו",
            wiz_live_failed: "נכשלו",
            wiz_live_status: "סטטוס",

            targets_title: "קבוצות שרתים",
            targets_subtitle: "רשימות יעדים שמורות לשימוש חוזר - במקום להקליד IP כל פעם",
            targets_new: "קבוצה חדשה",
            targets_empty: "אין עדיין קבוצות שמורות. לחץ על 'קבוצה חדשה' כדי להוסיף.",
            targets_servers: "שרתים",
            targets_run: "הרץ",
            targets_edit: "ערוך",
            targets_delete: "מחק",
            targets_delete_confirm: "האם למחוק את הקבוצה",
            targets_name_label: "שם הקבוצה",
            targets_ips_label: "כתובות IP (אחת בשורה)",
            targets_validation: "יש להזין שם ולפחות כתובת IP אחת",

            nav_scheduling: "תזמון",
            sched_title: "תזמון",
            sched_subtitle: "תזמון חד-פעמי של אוטומציות להרצה מאוחרת יותר",
            sched_new_btn: "תזמון חדש",
            sched_loading: "טוען תזמונים...",
            sched_empty: "אין עדיין תזמונים. לחץ על 'תזמון חדש' כדי להתחיל.",
            sched_th_automation: "אוטומציה",
            sched_th_servers: "שרתים",
            sched_th_time: "מועד מתוזמן",
            sched_th_status: "סטטוס",
            sched_th_creator: "נוצר על ידי",
            sched_th_actions: "פעולות",
            sched_status_pending: "ממתין",
            sched_status_triggered: "הופעל",
            sched_status_cancelled: "בוטל",
            sched_status_failed: "נכשל",
            sched_cancel_btn: "בטל",
            sched_edit_btn: "ערוך",
            sched_updated_toast: "מועד התזמון עודכן בהצלחה",
            sched_edit_failed: "עדכון התזמון נכשל",
            sched_cancel_confirm_title: "לבטל את התזמון?",
            sched_delete_confirm_title: "למחוק את הרשומה הזו לצמיתות?",
            sched_create_failed: "יצירת התזמון נכשלה",
            sched_created_toast: "האוטומציה תוזמנה בהצלחה",
            wiz_schedule_title: "תזמון להרצה מאוחרת",
            wiz_schedule_note: "בחרו תאריך ושעה בעתיד - האוטומציה תופעל אוטומטית במועד שנבחר, גם אם הדפדפן סגור.",
            wiz_schedule_dt_label: "תאריך ושעה",
            wiz_btn_schedule: "תזמן הרצה",
            wiz_schedule_blocked_destructive: "אוטומציות בסיכון הרסני לא ניתנות לתזמון - יש להריץ אותן באופן מיידי עם אישור מפורש.",
            wiz_schedule_pick_time: "יש לבחור תאריך ושעה בעתיד",

            reports_title: "דוחות (Outputs)"
        },
        en: {
            brand_title: "PS Automation",
            sidebar_scripts: "Available Automations",
            chromedriver_label: "Chromedriver Version:",
            lang_label: "Language / שפה:",
            theme_label: "Display Mode",
            theme_dark: "Dark",
            theme_light: "Light",
            logout_label: "Log out",
            settings_btn: "Settings",
            idle_warning_title: "You'll be logged out soon",
            idle_warning_body: "Due to inactivity you'll be signed out in about a minute. Click to stay signed in.",
            idle_warning_stay_btn: "Stay signed in",
            default_header_title: "Select Automation to Run",
            default_header_desc: "Select an automation from the list to configure and start running.",
            status_running: "Running Execution...",
            status_finished: "Execution Completed Successfully",
            status_failed: "Execution Failed",
            status_killed: "Execution Terminated by User",
            form_title: "Execution Parameters & Settings",
            form_placeholder: "Please select an automation from the sidebar to display setup options.",
            form_no_inputs: "This automation runs locally and needs no extra parameters. Click Run to start.",
            btn_run: "Run Active Automation",
            btn_kill: "Stop Execution Immediately",
            console_title: "Real-Time Execution Log (Console)",
            console_welcome: "> Run control system ready. Script output will appear here in real-time...",
            clear_console_tip: "Clear console",
            nav_dashboard: "Dashboard",
            nav_logs: "Logs",
            nav_files: "Outputs",
            logs_page_title: "Logs - Run History",
            files_page_title: "Outputs - Generated Files",
            btn_open_logs_folder: "Open Logs Folder",
            btn_open_output_folder: "Open Outputs Folder",
            btn_goto_logs: "Go to Logs",
            chrome_field_label: "Chrome Version (ChromeDriver):",
            edit_desc_tip: "Edit description",
            edit_desc_modal_title: "Edit Description",
            edit_desc_he_label: "Description in Hebrew",
            edit_desc_en_label: "Description in English",
            toast_desc_saved: "Description saved successfully",
            btn_open_run_log: "Open run log",
            tab_history: "Run History",
            tab_files: "Report Files",
            btn_open_reports: "Results Folder",
            btn_open_report_folder: "Report Folder",
            btn_refresh: "Refresh",
            btn_export: "Export to Excel",
            open_scripts_folder: "Open Scripts Folder",
            placeholder_history: "No execution history available.",
            placeholder_files: "No reports or logs found in directory.",
            th_automation: "Automation",
            th_date: "Date",
            th_start: "Start",
            th_duration: "Duration",
            th_user: "User",
            th_machine_ip: "Machine IP",
            th_servers: "Servers",
            th_status: "Status",
            th_result: "Log File",
            th_fail_reason: "Failure Reason",
            th_actions: "Actions",
            th_file_name: "File Name",
            th_modified: "Date Modified",
            th_size: "Size",
            btn_open: "Open",
            btn_reveal: "Reveal in Folder",
            btn_delete: "Delete",
            reports_delete_selected: "Delete Selected",
            logs_delete_selected: "Delete Selected",
            delete_confirm_title: "Confirm Delete",
            delete_report_confirm_body: "Are you sure you want to delete {n} report(s)? This cannot be undone.",
            delete_log_confirm_body: "Are you sure you want to delete {n} log entr(y/ies)? This cannot be undone.",
            delete_partial_failed: "Some files could not be deleted",
            toast_deleted: "Deleted successfully",
            btn_details: "Details",
            btn_run_again: "Run Again",
            status_completed_badge: "Completed",
            status_failed_badge: "Failed",
            status_killed_badge: "Killed",
            status_running_badge: "Running...",
            toast_start: "Starting automation script...",
            toast_success: "Execution finished successfully!",
            toast_failed: "Error! Execution failed.",
            toast_killed: "Execution stopped manually.",
            toast_reports_refreshed: "Reports list refreshed",
            toast_console_cleared: "Console cleared",
            toast_copied: "Summary copied to clipboard",
            toast_profile_saved: "Command profile saved",
            toast_file_opened: "File opened successfully",
            toast_run_again_loaded: "Form filled from a previous run - edit if needed and click Run",
            mode_label: "Server Input Method (IP Mode):",
            mode_range: "IP Range (Sequence)",
            mode_list: "Manual IP List (Custom List)",
            base_ip: "Base IP - first three octets only (e.g. 192.168.0):",
            start_suffix: "Starting Suffix (e.g. 207):",
            count: "Number of Servers to Process:",
            ips_list: "List of IP Addresses (One per line):",
            ips_list_current: "Current iDRAC IPs (one per line - the ones we connect to):",
            ips_range_hint: "You can also write a range on one line, e.g.: 192.168.0.1-192.168.0.3 (instead of 3 separate lines)",
            hostnames_list: "List of Hostnames (one per line; the first name maps to the first IP, and so on):",
            hostnames_range_hint: "You can also write a range on one line, e.g.: kafka1-kafka3 (instead of 3 separate lines)",
            newips_list: "iDRAC IP by NDD - new iDRAC IPs (one per line; the first maps to the first current iDRAC, and so on):",
            newips_range_hint: "You can also write a range on one line, e.g.: 192.168.0.101-192.168.0.103 (instead of 3 separate lines)",
            netmask_label: "Netmask (same for all servers - leave empty to keep unchanged):",
            netmask_hint: "e.g.: 255.255.255.0",
            gateway_label: "Gateway (same for all servers - leave empty to keep unchanged):",
            gateway_hint: "e.g.: 10.201.91.254",
            raid_name_label: "RAID (Virtual Disk) name to create:",
            raid_name_hint: "Default: vDisk1. Editable. A name is required - a RAID can't be created without one.",
            raid_name_required: "Please enter a RAID name (a RAID can't be created without one).",
            jt_servers: "servers",
            jt_state_pending: "Pending",
            jt_state_running: "Running",
            jt_state_success: "Success",
            jt_state_failed: "Failed",
            jt_stop: "Stop job",
            jt_close: "Close window",
            jt_open_live: "Open the live run screen",
            ts_title: "⏱ Time saved",
            ts_lead: "cumulative · all time",
            ts_unit_min: "Minutes",
            ts_unit_hour: "Hours",
            ts_unit_day: "Days",
            dns_list: "DNS servers to add (one per line) - inserted at the TOP of resolv.conf:",
            ntp_list: "NTP servers to add (one per line) - inserted at the TOP of chrony.conf (pool lines are removed):",
            username: "Username:",
            password: "Password:",
            password_show_tip: "Show password",
            password_hide_tip: "Hide password",
            use_default_creds: "Use default username & password credentials",
            yes_y: "Yes (y)",
            no_n: "No (n)",
            commands_label: "Commands to execute on remote server:",
            server_type_label: "Select Command Profile (Server Type):",
            custom_commands: "Custom",
            save_profile_btn: "+ Save as New Profile",
            save_profile_prompt: "Enter a name for the new server profile (e.g. MONGO):",
            delete_profile_confirm: "Delete profile",
            confirm_run_title: "Run Confirmation",
            confirm_run_body: "Start the automation:",
            confirm_run_servers: "Number of target servers:",
            confirm_driver_warning: "Warning: the selected ChromeDriver version does not match the installed Chrome version!",
            confirm_kill_title: "Stop Execution",
            confirm_kill_body: "Stop the active execution immediately? This will terminate all processes belonging to this run.",
            btn_confirm: "Confirm & Run",
            btn_confirm_kill: "Yes, stop now",
            btn_cancel: "Cancel",
            btn_save: "Save",
            translate_working: "Auto-translating...",
            translate_offline: "Auto-translate unavailable (no internet) - fill manually",
            progress_of: "of",
            stage_waiting: "Waiting for processing to start...",
            chip_all_servers: "All (merged)",
            summary_title: "Run Summary",
            summary_success_banner: "Execution completed successfully",
            summary_failed_banner: "Execution failed - see details below",
            summary_killed_banner: "Execution was stopped by the user",
            summary_what_ran: "What ran",
            summary_started: "Start time",
            summary_ended: "End time",
            summary_duration: "Total duration",
            summary_user: "Triggered by",
            summary_computer: "Computer",
            summary_servers_section: "Servers Processed",
            summary_success_actions: "Successful operations",
            summary_failed_actions: "Failed operations",
            summary_reports_section: "Generated reports & locations",
            summary_no_servers: "No per-server details were reported for this run.",
            summary_no_reports: "No report files were generated in this run.",
            summary_none: "None",
            btn_copy_summary: "Copy Summary",
            btn_open_all_reports: "Open Output Folder",
            toast_output_revealed: "Opened Outputs folder — this run's output is highlighted",
            details_title: "Run Details",
            details_end: "End time",
            details_computer: "Computer",
            details_servers: "Target servers",
            details_outputs: "Generated files",
            filter_date: "Filter by date",
            filter_type_all: "All process types",
            filter_server: "Filter by server...",
            filter_machine_ip: "Filter by machine IP...",
            filter_status_all: "All statuses",
            filter_search: "Free search...",
            filter_clear: "Clear Filters",
            chrome_detected: "Chrome installed:",
            chrome_not_detected: "Google Chrome installation was not detected",
            driver_recommended: "(Recommended - matches Chrome)",
            driver_no_match: "No ChromeDriver matches the installed Chrome version! Add a matching version to the Chromedrivers folder (no code change needed).",
            error_prefix: "Error",
            error_details: "Details",
            error_connection: "Connection error to the local server. Make sure server.py is running.",
            run_btn_tip: "Runs the selected automation with the configured parameters",
            kill_btn_tip: "Immediately stops the active run and all its child processes",
            version_label: "Version",
            entry_company_label: "Company",
            entry_stage_label: "Stage",
            entry_add_stage_btn: "+ Add Stage",
            entry_add_stage_prompt: "New stage name (e.g., Firewall):",
            add_automation_btn: "Add Automation",
            add_automation_modal_title: "Add New Automation",
            add_automation_intro: "Pick a destination in the folder tree, then choose a script file to move there.",
            add_automation_tree_label: "Destination in folder tree",
            add_automation_name_label: "Display name (optional)",
            add_automation_desc_he_label: "Description in Hebrew (optional)",
            add_automation_desc_en_label: "Description in English (optional)",
            add_automation_file_label: "Script file to move",
            add_automation_no_files: "No unassigned files of this language were found. Add a .py/.ps1 file directly into the Scripts folder and try again.",
            add_automation_no_dest: "Select a destination in the folder tree on the left",
            add_automation_save_btn: "Move & Add",
            no_automations_placeholder: "No automations yet for the current selection. Click 'Add Automation' to add one.",
            toast_stage_added: "Stage added successfully",
            toast_automation_added: "Automation moved and added successfully",
            run_again_confirm_title: "Run Again",
            run_again_confirm_body: "The previous run's parameters will be loaded into the run form. You can edit them before clicking Run. Continue?",
            run_again_unavailable: "This run can't be reloaded (missing data or the automation was removed).",

            // --- New shell: nav groups / pages (Dashboard/Wizard/Targets/Reports/Logs/Audit/Settings) ---
            nav_group_main: "Main",
            nav_group_results: "Results",
            nav_group_admin: "Admin",
            nav_wizard: "Run Automation",
            nav_targets: "Server Groups",
            nav_reports: "Reports",

            risk_read: "Read-only",
            risk_config: "Configuration",
            risk_destructive: "Destructive",
            wiz_cat_title: "Categories",
            cat_all: "All automations",
            cat_minutes: "min",
            cat_seconds: "sec",
            categories: {
                report: "Reports",
                configuration: "Configuration",
                power: "Power",
                storage: "Storage / RAID",
                network: "Network",
                validation: "Validation",
                redhat_validation: "RedHat Validation",
                windows: "Windows",
                general: "General"
            },
            dash_risk_count_suffix: "automations",
            dash_risk_caption: "Risk categories determine the approval level required before running.",

            dash_title: "Dashboard",
            dash_subtitle: "An overview of system status and runs",
            dash_new_run: "New run",
            dash_env_title: "Environment health",
            dash_env_disk: "Free disk space",
            dash_risk_title: "Automations by risk",
            dash_quick_title: "Quick actions",
            dash_recent_title: "Recent runs",
            dash_recent_all: "View all",
            dash_col_risk: "Risk",
            dash_stat_runs_today: "Runs today",
            dash_stat_success_rate: "Success rate",
            dash_stat_succeeded: "succeeded",
            dash_stat_screenshots: "Screenshots captured automatically",
            dash_stat_screenshots_sub: "Cumulative all-time (never drops when reports are deleted)",
            ss_clear_btn: "Clear",
            ss_clear_title: "Reset screenshot counter",
            ss_clear_prompt: "Enter password to reset the counter to zero:",
            ss_clear_placeholder: "Password",
            ss_clear_wrong: "Wrong password - counter not reset",
            ss_clear_done: "Screenshot counter reset",
            dash_stat_running: "Running now",

            wiz_title: "Run Automation",
            wiz_subtitle: "A guided wizard with pre-flight checks",
            wiz_step1_label: "Pick automation",
            wiz_step2_label: "Targets",
            wiz_step3_label: "Pre-flight checks",
            wiz_step4_label: "Confirm & run",
            wiz_search_label: "Search automations",
            wiz_pick_title: "Choose an automation to run",
            wiz_btn_continue: "Continue",
            wiz_targets_for: "Targets for",
            wiz_target_source_label: "Target source",
            wiz_src_manual: "Manual entry",
            wiz_src_group: "Saved group",
            wiz_btn_back: "Back",
            wiz_btn_continue_preflight: "Continue to checks",
            wiz_preflight_title: "Pre-flight checks",
            wiz_checking: "Checking...",
            wiz_pending: "Pending",
            wiz_check_addr: "Validate & normalize addresses",
            wiz_check_reach: "Reachability check (ping)",
            wiz_check_env: "Environment check",
            wiz_addr_valid: "valid addresses",
            wiz_addr_dupes: "duplicates",
            wiz_addr_invalid: "invalid",
            wiz_addr_none: "No addresses entered",
            wiz_reach_ok: "reachable",
            wiz_reach_warn: "server(s) did not respond to ping - they'll still be included in the run but may fail",
            wiz_reach_error: "Reachability check failed (proceed with caution)",
            wiz_env_ok: "OK",
            wiz_risk_all: "All risk levels",
            wiz_btn_continue_confirm: "Continue to confirm",
            wiz_confirm_title: "Summary & confirmation",
            wiz_live_label: "Now running:",
            wiz_more: "more",
            wiz_destructive_warning: "Destructive action! This cannot be undone.",
            wiz_type_confirm: "Type CONFIRM to approve this run.",
            wiz_confirm_placeholder: "Type CONFIRM",
            wiz_btn_run: "Run automation",
            wiz_live_total: "Total servers",
            wiz_live_done: "Completed",
            wiz_live_failed: "Failed",
            wiz_live_status: "Status",

            targets_title: "Server Groups",
            targets_subtitle: "Saved target lists for reuse - no need to retype IPs every time",
            targets_new: "New group",
            targets_empty: "No saved groups yet. Click 'New group' to add one.",
            targets_servers: "servers",
            targets_run: "Run",
            targets_edit: "Edit",
            targets_delete: "Delete",
            targets_delete_confirm: "Delete group",
            targets_name_label: "Group name",
            targets_ips_label: "IP addresses (one per line)",
            targets_validation: "A name and at least one IP address are required",

            nav_scheduling: "Scheduling",
            sched_title: "Scheduling",
            sched_subtitle: "Schedule a one-time automation run for later",
            sched_new_btn: "New schedule",
            sched_loading: "Loading schedules...",
            sched_empty: "No schedules yet. Click 'New schedule' to add one.",
            sched_th_automation: "Automation",
            sched_th_servers: "Servers",
            sched_th_time: "Scheduled time",
            sched_th_status: "Status",
            sched_th_creator: "Created by",
            sched_th_actions: "Actions",
            sched_status_pending: "Pending",
            sched_status_triggered: "Triggered",
            sched_status_cancelled: "Cancelled",
            sched_status_failed: "Failed",
            sched_cancel_btn: "Cancel",
            sched_edit_btn: "Edit",
            sched_updated_toast: "Schedule time updated successfully",
            sched_edit_failed: "Failed to update the schedule",
            sched_cancel_confirm_title: "Cancel this schedule?",
            sched_delete_confirm_title: "Permanently delete this entry?",
            sched_create_failed: "Failed to create the schedule",
            sched_created_toast: "Automation scheduled successfully",
            wiz_schedule_title: "Schedule for later",
            wiz_schedule_note: "Pick a future date and time - the automation will run automatically at that moment, even if the browser is closed.",
            wiz_schedule_dt_label: "Date and time",
            wiz_btn_schedule: "Schedule run",
            wiz_schedule_blocked_destructive: "Destructive-risk automations cannot be scheduled - they must be run immediately with explicit confirmation.",
            wiz_schedule_pick_time: "Please pick a future date and time",

            reports_title: "Reports (Outputs)"
        }
    };

    function t() {
        return TRANSLATIONS[currentLanguage];
    }

    // =====================================================================
    // Toast + centralized error reporting
    // =====================================================================
    function showToast(message, type = "success") {
        const container = document.getElementById("toast-container");
        const toast = document.createElement("div");
        toast.className = `toast ${type}`;
        toast.textContent = message;
        container.appendChild(toast);
        setTimeout(() => {
            toast.style.opacity = "0";
            setTimeout(() => toast.remove(), 400);
        }, type === "danger" ? 7000 : 3500);
    }

    function reportClientError(message, source, detail) {
        try {
            fetch("/api/client_error", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ message: String(message), source: String(source || "-"), detail: String(detail || "-") })
            }).catch(() => {});
        } catch (e) { /* logging must never break the app */ }
    }

    function showError(context, err) {
        const detail = (err && err.message) ? err.message : String(err);
        showToast(`${t().error_prefix}: ${context}\n${t().error_details}: ${detail}`, "danger");
        reportClientError(context, "app.js", detail);
    }

    window.addEventListener("error", (e) => {
        reportClientError(e.message, e.filename + ":" + e.lineno, "window.onerror");
    });

    window.addEventListener("unhandledrejection", (e) => {
        reportClientError("Unhandled promise rejection", "app.js", e.reason ? String(e.reason) : "-");
    });

    // =====================================================================
    // PROTECTED COPYRIGHT MODULE
    // =====================================================================
    const _0xa7 = [19, 84, 7, 42, 99, 120, 5, 61], _0xb3 = [88, 23, 44, 91, 77, 8, 130, 201];
    const _0xc9 = "0f0nGFNKMx0NZUUiLCjDs3I6bgpFWEBROXMMHyxu7Kgztoe+QzlpUXhFRTwlfPHpQTF0TxEOYFk=";
    const _0xd1 = "0f0nGFNKMx2Ph/vOmqBVUMTAJ/3Br5PqyMCMjNQoVVzExNC2tNrSrnjAv4zp3yIeh3TlqvdY0qaPiwyM2d8UHoiDkv36r5Dq8jf78pqWVVzE/NC/tNI=";

    function _0xdec(blob) {
        const k = _0xa7.concat(_0xb3);
        const raw = atob(blob);
        const out = new Uint8Array(raw.length);
        for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i) ^ k[i % k.length];
        return new TextDecoder("utf-8").decode(out);
    }

    let _crServer = null;
    function _crLine() {
        if (_crServer) return currentLanguage === "he" ? _crServer.he : _crServer.en;
        return currentLanguage === "he" ? _0xdec(_0xd1) : _0xdec(_0xc9);
    }

    function injectCopyright() {
        const container = document.getElementById("dynamic-copyright-container");
        if (!container) return;
        container.innerHTML = `<div class="copyright-box" id="copyBox">
            <div id="copyImg" class="copyright-text-line">${_crLine()}</div>
        </div>`;
        // Block selection/copy/context-menu/drag so the credit line can be read
        // but never highlighted or copied.
        const _cb = document.getElementById("copyBox");
        if (_cb) ["selectstart", "copy", "cut", "contextmenu", "dragstart"].forEach(function (ev) {
            _cb.addEventListener(ev, function (e) { e.preventDefault(); });
        });
    }

    fetch("/api/copyright").then(r => r.json()).then(d => {
        if (d && d.he && d.en) { _crServer = d; }
        injectCopyright();
    }).catch(() => injectCopyright());

    setInterval(() => {
        const box = document.getElementById("copyBox");
        let needsReinject = !box;
        if (box) {
            const style = window.getComputedStyle(box);
            if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
                box.style.setProperty("display", "block", "important");
                box.style.setProperty("visibility", "visible", "important");
                box.style.setProperty("opacity", "1", "important");
            }
            const textEl = document.getElementById("copyImg");
            if (!textEl || !textEl.textContent || textEl.textContent.length < 10) needsReinject = true;
        }
        if (needsReinject) injectCopyright();
    }, 3000);

    const _crObserver = new MutationObserver(() => {
        if (!document.getElementById("copyBox")) injectCopyright();
    });
    _crObserver.observe(document.body, { childList: true, subtree: true });

    // =====================================================================
    // Generic confirm modal
    // =====================================================================
    function showConfirm(title, body, okLabel, danger = false) {
        return new Promise((resolve) => {
            const overlay = document.getElementById("confirm-modal");
            const okBtn = document.getElementById("confirm-modal-ok");
            const cancelBtn = document.getElementById("confirm-modal-cancel");
            document.getElementById("confirm-modal-title").textContent = title;
            document.getElementById("confirm-modal-body").textContent = body;
            okBtn.textContent = okLabel;
            okBtn.className = danger ? "btn btn-danger btn-sm" : "btn btn-primary btn-sm";
            cancelBtn.textContent = t().btn_cancel;
            overlay.style.display = "flex";

            const cleanup = (result) => {
                overlay.style.display = "none";
                okBtn.onclick = null;
                cancelBtn.onclick = null;
                resolve(result);
            };
            okBtn.onclick = () => cleanup(true);
            cancelBtn.onclick = () => cleanup(false);
        });
    }

    // Lightweight password-entry modal (built on the fly). Resolves with the
    // typed password, or null if cancelled. Used to gate the screenshot-counter
    // reset behind a password.
    function promptPassword(title, prompt, placeholder) {
        return new Promise((resolve) => {
            const overlay = document.createElement("div");
            overlay.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:99999";
            overlay.innerHTML = `
              <div dir="rtl" style="background:var(--bg-card,#17273d);border:1px solid var(--border-glass,#2a3b55);border-radius:12px;padding:22px;max-width:400px;width:90%;box-shadow:0 12px 44px rgba(0,0,0,.55)">
                <div style="font-size:17px;font-weight:700;color:var(--text-primary,#fff);margin-bottom:8px">${title}</div>
                <div style="font-size:13px;color:var(--text-secondary,#adbdd0);margin-bottom:14px">${prompt}</div>
                <input type="password" class="form-control" placeholder="${placeholder || ''}" autocomplete="off" style="width:100%;margin-bottom:16px">
                <div style="display:flex;gap:8px;justify-content:flex-start">
                  <button class="btn btn-primary btn-sm" data-act="ok">${t().btn_confirm || 'OK'}</button>
                  <button class="btn btn-secondary btn-sm" data-act="cancel">${t().btn_cancel}</button>
                </div>
              </div>`;
            document.body.appendChild(overlay);
            const input = overlay.querySelector("input");
            input.focus();
            const cleanup = (val) => { overlay.remove(); resolve(val); };
            overlay.querySelector('[data-act="ok"]').onclick = () => cleanup(input.value);
            overlay.querySelector('[data-act="cancel"]').onclick = () => cleanup(null);
            overlay.addEventListener("click", (e) => { if (e.target === overlay) cleanup(null); });
            input.addEventListener("keydown", (e) => {
                if (e.key === "Enter") cleanup(input.value);
                else if (e.key === "Escape") cleanup(null);
            });
        });
    }

    // =====================================================================
    // Environment: app version, Chrome version, ChromeDriver matching.
    // The ChromeDriver picker is no longer a sidebar control - it renders
    // as a regular form field inside every script that needs it.
    // =====================================================================
    // Environment (Chrome/ChromeDriver/racadm/plink) is only fetched once per
    // login session and cached in sessionStorage - it doesn't change while the
    // user stays logged in, so a plain F5 refresh reuses the cached result
    // instead of re-checking (and briefly rendering stale/failed defaults
    // while the real check is still in flight). performLogout() clears the
    // cache so the next login/logout cycle re-reads it fresh.
    function loadEnvironment() {
        try {
            const cached = sessionStorage.getItem(ENV_CACHE_KEY);
            if (cached) {
                environmentInfo = JSON.parse(cached);
                renderEnvironmentPanel();
                return Promise.resolve();
            }
        } catch (e) { /* fall through to a fresh fetch */ }

        return fetch("/api/environment")
            .then(res => res.json())
            .then(env => {
                environmentInfo = env;
                try { sessionStorage.setItem(ENV_CACHE_KEY, JSON.stringify(env)); } catch (e) { /* ignore quota errors */ }
                renderEnvironmentPanel();
            })
            .catch(err => showError(t().error_connection, err));
    }

    function renderEnvironmentPanel() {
        const env = environmentInfo || {};
        updateCornerVersion();

        const ubName = document.getElementById("ub-name");
        const ubAvatar = document.getElementById("ub-avatar");
        const displayName = env.login_user || env.user || "Admin";
        if (ubName) ubName.textContent = displayName;
        if (ubAvatar) ubAvatar.textContent = displayName.charAt(0).toUpperCase();

        // In case this resolves after the Dashboard already rendered against
        // empty defaults (e.g. the very first login, before any cache exists).
        if (currentPage === "dashboard") renderEnvHealthRows();
    }

    function loadDefaultCredentials() {
        return fetch("/api/default-credentials")
            .then(res => res.json())
            .then(d => { if (d && !d.error) defaultCreds = d; })
            .catch(() => {});
    }

    // Builds the "Chrome version" form field (select + detected-Chrome help
    // line + mismatch warning) for scripts whose inputs include "chromedriver".
    function buildChromeDriverField(prefill) {
        const tr = t();
        const env = environmentInfo || {};
        const drivers = env.drivers || [];
        const chromeMajor = env.chrome_major;

        let chosen = "";
        if (prefill && drivers.some(d => d.path === prefill)) {
            chosen = prefill;
        } else if (prefs.last_driver && drivers.some(d => d.path === prefs.last_driver)) {
            chosen = prefs.last_driver;
        } else if (chromeMajor) {
            const match = drivers.find(d => d.major === chromeMajor);
            if (match) chosen = match.path;
        }
        if (!chosen && drivers.length) chosen = drivers[0].path;

        const options = drivers.length
            ? drivers.map(d =>
                `<option value="${d.path}" data-major="${d.major || ""}" ${d.path === chosen ? "selected" : ""}>${d.name}${chromeMajor && d.major === chromeMajor ? " " + tr.driver_recommended : ""}</option>`
              ).join("")
            : `<option value="">${currentLanguage === "he" ? "לא נמצאו דרייברים" : "No drivers found"}</option>`;

        const chromeLine = env.chrome_version
            ? `${tr.chrome_detected} ${env.chrome_version}`
            : tr.chrome_not_detected;
        const warn = (env.chrome_version && env.has_matching_driver === false)
            ? `<div class="form-help chrome-field-warning">⚠ ${tr.driver_no_match}</div>`
            : "";

        const block = document.createElement("div");
        block.className = "form-group";
        block.innerHTML = `
            <label for="chromedriver_path">${tr.chrome_field_label} <span class="required">*</span></label>
            <select id="chromedriver_path" name="chromedriver_path" class="form-control">${options}</select>
            <div class="form-help">${chromeLine}</div>
            ${warn}
        `;
        block.querySelector("select").addEventListener("change", (e) => {
            savePref("last_driver", e.target.value);
            prefs = loadPrefs();
        });
        return block;
    }

    // =====================================================================
    // Command profiles (server types)
    // =====================================================================
    function loadCommandProfiles() {
        return fetch("/api/command_profiles")
            .then(res => res.json())
            .then(profiles => { commandProfiles = profiles || {}; })
            .catch(err => showError("Command profiles", err));
    }

    function profileCommands(key) {
        if (key === "custom") {
            return prefs.last_custom_commands || "# Enter custom validation commands here\nhostname\ndate";
        }
        return (commandProfiles[key] && commandProfiles[key].commands) || "";
    }

    // =====================================================================
    // Language handling (full translation + RTL/LTR direction)
    // =====================================================================
    function translateUI() {
        const lang = currentLanguage;
        const tr = t();

        document.documentElement.lang = lang;
        document.documentElement.dir = lang === "he" ? "rtl" : "ltr";

        document.getElementById("brand-app-title").textContent = tr.brand_title;
        const langBtn = document.getElementById("lang-toggle");
        if (langBtn) langBtn.title = tr.lang_label;
        document.getElementById("theme-toggle").title = tr.theme_label;
        updateThemeButtons();
        updateLangButton();
        updateCornerVersion();
        updateLogoutButton();

        document.getElementById("add-automation-text").textContent = tr.add_automation_btn;
        if (companiesData && Object.keys(companiesData).length) renderWizardCompanySelect();

        // Sidebar nav
        document.getElementById("ng-main").textContent = tr.nav_group_main;
        document.getElementById("ng-results").textContent = tr.nav_group_results;
        document.getElementById("nav-dashboard-text").textContent = tr.nav_dashboard;
        document.getElementById("nav-wizard-text").textContent = tr.nav_wizard;
        document.getElementById("nav-targets-text").textContent = tr.nav_targets;
        document.getElementById("nav-scheduling-text").textContent = tr.nav_scheduling;
        document.getElementById("nav-reports-text").textContent = tr.nav_reports;
        document.getElementById("nav-logs-text").textContent = tr.nav_logs;

        // Dashboard
        document.getElementById("dash-title").textContent = tr.dash_title;
        document.getElementById("dash-subtitle").textContent = tr.dash_subtitle;
        document.getElementById("dash-new-run-text").textContent = tr.dash_new_run;
        document.getElementById("dash-env-title").textContent = tr.dash_env_title;
        document.getElementById("dash-risk-title").textContent = tr.dash_risk_title;
        document.getElementById("dash-quick-title").textContent = tr.dash_quick_title;
        document.getElementById("dash-qa-run").textContent = tr.nav_wizard;
        document.getElementById("dash-qa-reports").textContent = tr.nav_reports;
        document.getElementById("dash-qa-logs").textContent = tr.nav_logs;
        document.getElementById("dash-qa-targets").textContent = tr.nav_targets;
        document.getElementById("dash-recent-title").textContent = tr.dash_recent_title;
        document.getElementById("dash-recent-all").textContent = tr.dash_recent_all;

        // Wizard
        document.getElementById("wiz-title").textContent = tr.wiz_title;
        document.getElementById("wiz-subtitle").textContent = tr.wiz_subtitle;
        document.getElementById("wiz-company-label").textContent = tr.entry_company_label;
        document.getElementById("wiz-stage-label").textContent = tr.entry_stage_label;
        document.getElementById("wiz-add-stage-text").textContent = tr.entry_add_stage_btn;
        document.getElementById("wiz-search-label").textContent = tr.wiz_search_label;
        document.getElementById("wiz-search-input").placeholder = "🔍 " + tr.wiz_search_label;
        document.getElementById("wiz-cat-title").textContent = tr.wiz_cat_title;
        document.getElementById("wiz-btn-continue").textContent = tr.wiz_btn_continue;
        // Re-render the catalog's category sidebar so its labels follow the
        // language switch.
        renderCategorySidebar();
        document.getElementById("wiz-targets-for").textContent = tr.wiz_targets_for;
        document.getElementById("wiz-target-source-label").textContent = tr.wiz_target_source_label;
        document.getElementById("wiz-src-manual").textContent = tr.wiz_src_manual;
        document.getElementById("wiz-src-group").textContent = tr.wiz_src_group;
        document.getElementById("wiz-btn-back-1").textContent = tr.wiz_btn_back;
        document.getElementById("wiz-btn-continue-preflight").textContent = tr.wiz_btn_continue_preflight;
        document.getElementById("wiz-preflight-title").textContent = tr.wiz_preflight_title;
        document.getElementById("wiz-btn-back-2").textContent = tr.wiz_btn_back;
        document.getElementById("wiz-btn-continue-confirm").textContent = tr.wiz_btn_continue_confirm;
        document.getElementById("wiz-confirm-title").textContent = tr.wiz_confirm_title;
        document.getElementById("wiz-live-label").textContent = tr.wiz_live_label;
        document.getElementById("wiz-btn-back-3").textContent = tr.wiz_btn_back;
        document.getElementById("wiz-btn-run").textContent = tr.wiz_btn_run;
        renderWizardSteps();

        document.getElementById("kill-btn-text").textContent = tr.btn_kill;
        document.getElementById("console-card-title").textContent = tr.console_title;
        const welcome = document.getElementById("console-welcome-line");
        if (welcome) welcome.textContent = tr.console_welcome;

        // Targets
        document.getElementById("targets-title").textContent = tr.targets_title;
        document.getElementById("targets-subtitle").textContent = tr.targets_subtitle;
        document.getElementById("targets-new-text").textContent = tr.targets_new;

        // Scheduling
        document.getElementById("sched-title").textContent = tr.sched_title;
        document.getElementById("sched-subtitle").textContent = tr.sched_subtitle;
        document.getElementById("sched-new-text").textContent = tr.sched_new_btn;
        document.getElementById("sched-loading-text").textContent = tr.sched_loading;
        document.getElementById("wiz-schedule-title").textContent = "🕒 " + tr.wiz_schedule_title;
        document.getElementById("wiz-schedule-note").textContent = tr.wiz_schedule_note;
        document.getElementById("wiz-schedule-dt-label").textContent = tr.wiz_schedule_dt_label;
        // Keep the main run/schedule button label in sync with the toggle
        // state after a language switch (it's set dynamically, not statically).
        if (currentPage === "wizard") updateWizScheduleUI();

        // Reports / Logs
        document.getElementById("reports-title").textContent = tr.reports_title;
        document.getElementById("open-reports-folder-text").textContent = tr.btn_open_output_folder;
        document.getElementById("logs-page-title").textContent = tr.logs_page_title;
        document.getElementById("export-history-text").textContent = tr.btn_export;
        document.getElementById("open-report-folder-text").textContent = tr.btn_open_logs_folder;
        updateBulkDeleteBtn("reports");
        updateBulkDeleteBtn("logs");

        document.getElementById("refresh-history-btn").title = tr.btn_refresh;
        document.getElementById("refresh-files-btn").title = tr.btn_refresh;

        document.getElementById("kill-btn").title = tr.kill_btn_tip;
        document.getElementById("open-scripts-folder-btn").title = tr.open_scripts_folder;
        document.getElementById("clear-console-btn").title = tr.clear_console_tip;
        document.getElementById("add-automation-btn").title = tr.add_automation_btn;

        renderEnvironmentPanel();
        if (selectedScript) renderForm(selectedScript);
        renderWizardAutoList(filteredScriptsForSearch());
        renderHistoryFilters();
        loadReportsAndHistory();
        if (currentPage === "dashboard") { renderEnvHealthRows(); renderRiskBreakdown(); }
        if (currentPage === "targets") renderTargetsGrid();
        renderJobTray();   // refresh the live job window labels + flip its side (LTR/RTL)
        renderTimeSaved(); // refresh the "time saved" squares in the current language
        if (dashAnalytics) renderDashCharts();   // re-label + redraw dashboard charts
        injectCopyright();
    }

    // Language toggle - a topbar button that flips he <-> en, mirroring the
    // dark/light theme button next to it (same class, same behavior pattern).
    function updateLangButton() {
        const btn = document.getElementById("lang-toggle");
        if (btn) btn.textContent = currentLanguage === "he" ? "🌐 עברית" : "🌐 English";
    }

    // App version pinned to the bottom start-corner of every page: the CSS
    // uses inset-inline-start, so it sits bottom-RIGHT in Hebrew (RTL) and
    // bottom-LEFT in English (LTR) automatically.
    function updateCornerVersion() {
        const el = document.getElementById("corner-version");
        if (!el) return;
        const v = (environmentInfo && environmentInfo.app_version) ? environmentInfo.app_version : "";
        if (!v) { el.textContent = ""; return; }
        el.textContent = currentLanguage === "he" ? ("גרסה " + v) : ("Version " + v);
    }

    const langToggleBtn = document.getElementById("lang-toggle");
    if (langToggleBtn) {
        langToggleBtn.addEventListener("click", () => {
            currentLanguage = currentLanguage === "he" ? "en" : "he";
            localStorage.setItem("app_lang", currentLanguage);
            savePref("lang", currentLanguage);
            translateUI();
        });
    }

    function scriptDescription(script) {
        if (currentLanguage === "en" && script.description_en) return script.description_en;
        return script.description;
    }

    // =====================================================================
    // Company / stage selection - lives inline in the Wizard page (step 1),
    // no separate entry gate. Automations of both languages are listed
    // together for the chosen company/stage; each is tagged Python/
    // PowerShell individually (see riskIcon/renderWizardAutoList).
    // =====================================================================
    function companyLabel(key) {
        const c = companiesData[key];
        if (!c) return key;
        return currentLanguage === "en" ? (c.label_en || c.label) : c.label;
    }

    function stageLabel(companyKey, stageKey) {
        const c = companiesData[companyKey];
        if (!c) return stageKey;
        const stage = (c.stages || []).find(s => s.key === stageKey);
        if (!stage) return stageKey;
        return currentLanguage === "en" ? (stage.label_en || stage.label) : stage.label;
    }

    function loadCompanies() {
        return fetch("/api/companies")
            .then(res => res.json())
            .then(data => {
                companiesData = data || {};
                renderWizardCompanySelect();
            })
            .catch(err => showError("Companies", err));
    }

    function renderWizardCompanySelect() {
        const select = document.getElementById("wiz-company-select");
        if (!select) return;
        const previous = ctx.company || prefs.last_company || "";
        select.innerHTML = "";
        Object.keys(companiesData).forEach(key => {
            const opt = document.createElement("option");
            opt.value = key;
            opt.textContent = companyLabel(key);
            select.appendChild(opt);
        });
        const validPrevious = (previous && companiesData[previous]) ? previous : (Object.keys(companiesData)[0] || "");
        select.value = validPrevious;
        ctx.company = validPrevious;
        renderWizardStageSelect();
    }

    function renderWizardStageSelect() {
        const select = document.getElementById("wiz-company-select");
        const stageGroup = document.getElementById("wiz-stage-group");
        const stageSelect = document.getElementById("wiz-stage-select");
        const company = select.value;
        const companyDef = companiesData[company];

        if (!companyDef || !companyDef.has_stages) {
            stageGroup.style.display = "none";
            stageSelect.innerHTML = "";
            ctx.stage = "";
            onWizardContextChanged();
            return;
        }

        stageGroup.style.display = "block";
        const previous = ctx.company === company ? (ctx.stage || prefs.last_stage) : prefs.last_stage;
        stageSelect.innerHTML = "";
        (companyDef.stages || []).forEach(stage => {
            const opt = document.createElement("option");
            opt.value = stage.key;
            opt.textContent = currentLanguage === "en" ? (stage.label_en || stage.label) : stage.label;
            stageSelect.appendChild(opt);
        });
        const stages = companyDef.stages || [];
        const validStage = (previous && stages.some(s => s.key === previous)) ? previous : ((stages[0] || {}).key || "");
        stageSelect.value = validStage;
        ctx.stage = validStage;
        onWizardContextChanged();
    }

    function onWizardContextChanged() {
        savePref("last_company", ctx.company);
        savePref("last_stage", ctx.stage);
        prefs = loadPrefs();
        selectedScript = null;
        const nextBtn = document.getElementById("wiz-step1-next");
        if (nextBtn) nextBtn.disabled = true;
        scriptsListSignature = "";
        fetchScriptsList(true);
    }

    document.getElementById("wiz-company-select").addEventListener("change", (e) => {
        ctx.company = e.target.value;
        renderWizardStageSelect();
    });
    document.getElementById("wiz-stage-select").addEventListener("change", (e) => {
        ctx.stage = e.target.value;
        onWizardContextChanged();
    });

    document.getElementById("wiz-add-stage-btn").addEventListener("click", () => {
        const company = document.getElementById("wiz-company-select").value;
        if (!company) return;
        const label = window.prompt(t().entry_add_stage_prompt);
        if (!label || !label.trim()) return;
        fetch(`/api/companies/${company}/stages`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ label: label.trim() })
        })
            .then(r => r.json())
            .then(d => {
                if (d.error) { showError(d.error, ""); return; }
                companiesData = d.companies;
                renderWizardStageSelect();
                document.getElementById("wiz-stage-select").value = d.key;
                ctx.stage = d.key;
                onWizardContextChanged();
                showToast(t().toast_stage_added, "success");
            })
            .catch(err => showError("Add stage", err));
    });

    function showDashboard() {
        switchPage(currentPage);
    }

    // =====================================================================
    // Page navigation (Dashboard / Wizard / Targets / Reports / Logs / Audit /
    // Settings) via the labeled sidebar. The current page is reflected in the
    // URL hash so a browser refresh brings you back to the same page.
    // =====================================================================
    const ALL_PAGES = ["dashboard", "wizard", "targets", "scheduling", "reports", "logs"];

    function switchPage(page, opts) {
        opts = opts || {};
        if (!ALL_PAGES.includes(page)) page = "dashboard";
        currentPage = page;

        ALL_PAGES.forEach(p => {
            const el = document.getElementById("page-" + p);
            if (el) el.style.display = p === page ? "block" : "none";
        });

        document.querySelectorAll(".side-nav-item").forEach(item => {
            item.classList.toggle("active", item.dataset.page === page);
        });

        // pushState (not replaceState) so every page switch is a real,
        // separate browser-history entry - the physical Back/Forward buttons
        // then step through Dashboard -> Wizard -> Reports etc. one at a
        // time, instead of collapsing the whole app session into a single
        // entry (which made Back jump straight past the app to whatever real
        // page came before it, e.g. the login page). Skipped when we're
        // RESTORING a page because the user just pressed Back/Forward
        // themselves (popstate below) - the browser already moved the
        // history position in that case, so pushing again would duplicate it.
        if (!opts.fromPopState) {
            try { history.pushState({ page: page }, "", "#" + page); } catch (e) { /* ignore */ }
        }

        if (page === "dashboard") loadDashboard();
        if (page === "wizard") enterWizardPage();
        if (page === "targets") loadTargetsPage();
        if (page === "scheduling") loadSchedulingPage();
        if (page === "reports" || page === "logs") loadReportsAndHistory();
    }

    // Restores the correct page when the user clicks the browser's physical
    // Back/Forward buttons (which change the URL hash without our own code
    // running) - reads whatever hash the browser just navigated to and
    // renders that page, without pushing yet another history entry.
    window.addEventListener("popstate", () => {
        const hash = (location.hash || "").replace(/^#/, "");
        switchPage(hash || "dashboard", { fromPopState: true });
    });

    document.querySelectorAll(".side-nav-item").forEach(item => {
        item.addEventListener("click", () => {
            // Clicking "Run Automation" in the sidebar always jumps back to
            // the automation catalog (step 1), even mid-flow (steps 2-4) -
            // otherwise the only way back there was the wizard's own Back
            // button. Skipped while a run is actively executing so this
            // never yanks the user away from the live progress view (that
            // takes priority regardless, see enterWizardPage()).
            if (item.dataset.page === "wizard" && !activeRunId) {
                WIZ.finished = true;
            }
            switchPage(item.dataset.page);
        });
    });

    document.querySelectorAll("[data-goto]").forEach(btn => {
        btn.addEventListener("click", () => switchPage(btn.getAttribute("data-goto")));
    });

    function stopHistoryPolling() {
        if (historyPollTimer) { clearInterval(historyPollTimer); historyPollTimer = null; }
    }

    function startHistoryPolling() {
        stopHistoryPolling();
        // Keeps the Logs/Audit/Dashboard pages current in real time (running
        // -> completed/failed/killed) without the user needing to refresh.
        historyPollTimer = setInterval(() => {
            if (["logs", "dashboard"].includes(currentPage)) loadReportsAndHistory();
        }, 4000);
    }

    // =====================================================================
    // Dashboard page - real stats, environment health, risk breakdown,
    // recent runs (all computed from /api/history, /api/environment and the
    // unscoped /api/scripts catalog - no mock data).
    // =====================================================================
    let screenshotCount = null;

    function loadDashboard() {
        loadAllScripts().then(() => renderRiskBreakdown());
        renderEnvHealthRows();
        loadReportsAndHistory();   // fetches history -> triggers renderDashboardStats()
        fetch("/api/stats/screenshots")
            .then(r => r.json())
            .then(d => { screenshotCount = (typeof d.count === "number") ? d.count : null; renderDashboardStats(); })
            .catch(() => {});
        loadTimeSaved();
        loadDashboardAnalytics();
    }

    // =====================================================================
    // Dashboard analytics charts (Grafana-style). All data comes from
    // /api/stats/analytics; charts are inline SVG (no libraries).
    // =====================================================================
    const DNS_ = "http://www.w3.org/2000/svg";
    function dEl(t, a, p) { const e = document.createElementNS(DNS_, t); for (const k in a) e.setAttribute(k, a[k]); if (p) p.appendChild(e); return e; }
    function dCv(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888"; }
    function dTTel() { let el = document.getElementById("dash-tt"); if (!el) { el = document.createElement("div"); el.id = "dash-tt"; document.body.appendChild(el); } return el; }
    function dShowTT(html, x, y) { const el = dTTel(); el.innerHTML = html; el.style.opacity = 1; el.style.left = (x + 14) + "px"; el.style.top = (y + 14) + "px"; }
    function dHideTT() { const el = document.getElementById("dash-tt"); if (el) el.style.opacity = 0; }
    function dSpark(svg, vals, color, fill) {
        svg.innerHTML = ""; const vb = svg.viewBox.baseVal, W = vb.width, H = vb.height, p = 4;
        if (!vals.length) return;
        const mx = Math.max(...vals), mn = Math.min(...vals), x = i => p + i * (W - 2 * p) / (Math.max(1, vals.length - 1)), y = v => p + (1 - (v - mn) / ((mx - mn) || 1)) * (H - 2 * p);
        let d = "M" + x(0) + " " + y(vals[0]); vals.forEach((v, i) => { if (i) d += " L" + x(i) + " " + y(v); });
        if (fill) dEl("path", { d: d + ` L${x(vals.length - 1)} ${H - p} L${x(0)} ${H - p} Z`, fill: color, "fill-opacity": .12 }, svg);
        dEl("path", { d, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", "vector-effect": "non-scaling-stroke" }, svg);
        dEl("circle", { cx: x(vals.length - 1), cy: y(vals[vals.length - 1]), r: 3.2, fill: color, stroke: dCv("--bg-card"), "stroke-width": 2 }, svg);
    }
    function dRollupHe(min) {
        min = Math.max(0, Math.round(min)); const d = Math.floor(min / 1440), h = Math.floor((min % 1440) / 60), m = min % 60;
        const M = m === 1 ? "דקה אחת" : m === 2 ? "שתי דקות" : m + " דקות", H = h === 1 ? "שעה אחת" : h === 2 ? "שעתיים" : h + " שעות", D = d === 1 ? "יום אחד" : d === 2 ? "יומיים" : d + " ימים";
        if (d > 0) return `${D}, ${H}, ${M}`; if (h > 0) return `${H} ו-${M}`; return M;
    }
    function dRollupEn(min) {
        min = Math.max(0, Math.round(min)); const d = Math.floor(min / 1440), h = Math.floor((min % 1440) / 60), m = min % 60;
        const u = (n, s) => n + " " + s + (n === 1 ? "" : "s");
        if (d > 0) return `${u(d, "day")}, ${u(h, "hour")}, ${u(m, "minute")}`; if (h > 0) return `${u(h, "hour")} and ${u(m, "minute")}`; return u(m, "minute");
    }

    function loadDashboardAnalytics() {
        fetch("/api/stats/analytics")
            .then(r => r.json())
            .then(d => { dashAnalytics = d; renderDashCharts(); })
            .catch(() => {});
        if (!dashRangeBound) {
            const rng = document.getElementById("dash-range");
            if (rng) {
                rng.addEventListener("click", e => {
                    if (e.target.tagName !== "BUTTON") return;
                    rng.querySelectorAll("button").forEach(b => b.classList.remove("active"));
                    e.target.classList.add("active");
                    dashRangeDays = +e.target.dataset.d || 14;
                    if (dashAnalytics) dRenderRuns();
                });
                dashRangeBound = true;
            }
        }
    }

    // sets bilingual panel titles + renders every chart from dashAnalytics
    function renderDashCharts() {
        if (!dashAnalytics) return;
        const he = currentLanguage === "he";
        const T = {
            hero: he ? "⏱ זמן שנחסך" : "⏱ Time saved",
            runs: he ? "📈 הרצות לאורך זמן" : "📈 Runs over time",
            gauge: he ? "✓ אחוז הצלחה" : "✓ Success rate",
            gaugeSub: he ? "מכלל ההרצות שהסתיימו" : "of all finished runs",
            weeks: he ? "🟢 זמן שנחסך לפי שבוע" : "🟢 Time saved per week",
            weeksSub: he ? "8 השבועות האחרונים (דקות)" : "last 8 weeks (minutes)",
            cat: he ? "📊 הרצות לפי קטגוריה" : "📊 Runs by category",
            catSub: he ? "מספר הרצות" : "run count",
            risk: he ? "🛡️ הרצות לפי רמת סיכון" : "🛡️ Runs by risk level",
            riskSub: he ? "חלוקת ההרצות" : "share of runs",
        };
        const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
        set("dash-hero-label", T.hero); set("dash-runs-title", T.runs);
        set("dash-gauge-title", T.gauge); set("dash-gauge-cap2", T.gaugeSub);
        set("dash-weeks-title", T.weeks); set("dash-weeks-sub", T.weeksSub);
        set("dash-cat-title", T.cat); set("dash-cat-sub", T.catSub);
        set("dash-risk-title", T.risk); set("dash-risk-sub", T.riskSub);
        dRenderHero(); dRenderRuns(); dRenderGauge(); dRenderWeeks(); dRenderCategories(); dRenderRisk();
    }

    function dRenderHero() {
        const a = dashAnalytics.totals || {}; const he = currentLanguage === "he";
        const min = a.time_saved_min || 0;
        const v = document.getElementById("dash-hero-value"); if (v) v.textContent = he ? dRollupHe(min) : dRollupEn(min);
        const s = document.getElementById("dash-hero-sub");
        if (s) s.textContent = he
            ? `סה"כ ${min.toLocaleString()} דקות · ${(a.runs || 0).toLocaleString()} הרצות · ${(a.servers || 0).toLocaleString()} שרתים · ממוצע ${a.avg_dur || "0:00"}`
            : `Total ${min.toLocaleString()} minutes · ${(a.runs || 0).toLocaleString()} runs · ${(a.servers || 0).toLocaleString()} servers · avg ${a.avg_dur || "0:00"}`;
        const spark = document.getElementById("dash-hero-spark");
        if (spark) { let acc = 0; const cum = (dashAnalytics.weekly || []).map(w => acc += w); dSpark(spark, cum.length ? cum : [0, 0], dCv("--accent-success"), true); }
    }

    function dRenderRuns() {
        const svg = document.getElementById("dash-runs-chart"); if (!svg) return; svg.innerHTML = "";
        const vb = svg.viewBox.baseVal, W = vb.width, H = vb.height, pad = { l: 12, r: 10, t: 14, b: 26 };
        const all = dashAnalytics.daily || []; const vals = all.slice(-dashRangeDays); const n = vals.length;
        if (!n) return;
        const max = Math.max(2, Math.ceil(Math.max(...vals) / 2) * 2 + 2);
        const x = i => pad.l + i * (W - pad.l - pad.r) / Math.max(1, n - 1), y = v => pad.t + (1 - v / max) * (H - pad.t - pad.b);
        const grid = dCv("--border-glass"), info = dCv("--accent-info"), card = dCv("--bg-card");
        for (let g = 0; g <= 4; g++) { const val = Math.round(max * g / 4), yy = y(val); dEl("line", { x1: pad.l, y1: yy, x2: W - pad.r, y2: yy, stroke: grid, "stroke-width": 1, "vector-effect": "non-scaling-stroke" }, svg); dEl("text", { x: pad.l - 3, y: yy - 3, class: "axis-label" }, svg).textContent = val; }
        let d = "M" + x(0) + " " + y(vals[0]); vals.forEach((v, i) => { if (i) d += " L" + x(i) + " " + y(v); });
        dEl("path", { d: d + ` L${x(n - 1)} ${y(0)} L${x(0)} ${y(0)} Z`, fill: info, "fill-opacity": .13 }, svg);
        dEl("path", { d, fill: "none", stroke: info, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", "vector-effect": "non-scaling-stroke" }, svg);
        const step = Math.max(1, Math.round(n / 6)); for (let i = 0; i < n; i += step) dEl("text", { x: x(i), y: H - 8, class: "axis-label", "text-anchor": "middle" }, svg).textContent = (n - i) + "d";
        dEl("text", { x: x(n - 1), y: H - 8, class: "axis-label", "text-anchor": "end" }, svg).textContent = currentLanguage === "he" ? "היום" : "now";
        dEl("circle", { cx: x(n - 1), cy: y(vals[n - 1]), r: 4, fill: info, stroke: card, "stroke-width": 2 }, svg);
        const hvl = dEl("line", { x1: 0, y1: pad.t, x2: 0, y2: H - pad.b, stroke: grid, "stroke-width": 1, opacity: 0, "vector-effect": "non-scaling-stroke" }, svg);
        const hd = dEl("circle", { r: 4.5, fill: info, stroke: card, "stroke-width": 2, opacity: 0 }, svg);
        const rc = dEl("rect", { x: 0, y: 0, width: W, height: H, fill: "transparent" }, svg);
        rc.addEventListener("mousemove", ev => { const b = svg.getBoundingClientRect(); let i = Math.round(((ev.clientX - b.left) / b.width * W - pad.l) / ((W - pad.l - pad.r) / Math.max(1, n - 1))); i = Math.max(0, Math.min(n - 1, i)); hvl.setAttribute("x1", x(i)); hvl.setAttribute("x2", x(i)); hvl.setAttribute("opacity", 1); hd.setAttribute("cx", x(i)); hd.setAttribute("cy", y(vals[i])); hd.setAttribute("opacity", 1); dShowTT(`<div class="tt-k">${currentLanguage === "he" ? "לפני " + (n - 1 - i) + " ימים" : (n - 1 - i) + "d ago"}</div><div class="tt-v">${vals[i]} ${currentLanguage === "he" ? "הרצות" : "runs"}</div>`, ev.clientX, ev.clientY); });
        rc.addEventListener("mouseleave", () => { hvl.setAttribute("opacity", 0); hd.setAttribute("opacity", 0); dHideTT(); });
        const sub = document.getElementById("dash-runs-sub"); if (sub) sub.textContent = currentLanguage === "he" ? `${dashRangeDays} ימים אחרונים · ${vals.reduce((a, b) => a + b, 0)} סה"כ` : `last ${dashRangeDays} days · ${vals.reduce((a, b) => a + b, 0)} total`;
    }

    function dRenderGauge() {
        const svg = document.getElementById("dash-gauge"); if (!svg) return; svg.innerHTML = "";
        const a = dashAnalytics.totals || {}, val = (a.success_rate || 0) / 100;
        const cx = 110, cy = 115, r = 88, th = 16, st = Math.PI * 0.8, en = Math.PI * 2.2;
        const col = val >= .9 ? dCv("--accent-success") : val >= .7 ? dCv("--accent-warning") : dCv("--accent-danger");
        const arc = (a0, a1, c, op) => { const lg = (a1 - a0) > Math.PI ? 1 : 0, P = (aa, rr) => [cx + rr * Math.cos(aa), cy + rr * Math.sin(aa)]; const [x0, y0] = P(a0, r), [x1, y1] = P(a1, r), [x2, y2] = P(a1, r - th), [x3, y3] = P(a0, r - th); dEl("path", { d: `M${x0} ${y0} A${r} ${r} 0 ${lg} 1 ${x1} ${y1} L${x2} ${y2} A${r - th} ${r - th} 0 ${lg} 0 ${x3} ${y3} Z`, fill: c, "fill-opacity": op }, svg); };
        arc(st, en, dCv("--text-main"), .06); if (val > 0) arc(st, st + (en - st) * val, col, .95);
        const gv = document.getElementById("dash-gauge-val"); if (gv) { gv.textContent = (a.success_rate || 0) + "%"; gv.style.color = col; }
        const gc = document.getElementById("dash-gauge-cap"); if (gc) gc.textContent = currentLanguage === "he" ? `${(a.runs || 0) - (a.failed || 0)}/${a.runs || 0} הרצות הצליחו` : `${(a.runs || 0) - (a.failed || 0)}/${a.runs || 0} runs succeeded`;
    }

    function dRenderWeeks() {
        const svg = document.getElementById("dash-weeks-chart"); if (!svg) return; svg.innerHTML = "";
        const vb = svg.viewBox.baseVal, W = vb.width, H = vb.height, pad = { l: 12, r: 10, t: 16, b: 24 };
        const vals = dashAnalytics.weekly || []; const n = vals.length; if (!n) return;
        const max = Math.max(10, Math.ceil(Math.max(...vals) / 100) * 100), slot = (W - pad.l - pad.r) / n, bw = Math.min(24, slot - 8);
        const y = v => pad.t + (1 - v / max) * (H - pad.t - pad.b), grid = dCv("--border-glass"), green = dCv("--accent-success");
        for (let g = 0; g <= 3; g++) { const val = Math.round(max * g / 3), yy = y(val); dEl("line", { x1: pad.l, y1: yy, x2: W - pad.r, y2: yy, stroke: grid, "stroke-width": 1, "vector-effect": "non-scaling-stroke" }, svg); dEl("text", { x: pad.l - 3, y: yy - 3, class: "axis-label" }, svg).textContent = val; }
        const mxv = Math.max(...vals);
        vals.forEach((v, i) => {
            const cx = pad.l + slot * i + slot / 2, x0 = cx - bw / 2, y0 = y(v);
            const rr = dEl("rect", { x: x0, y: y0, width: bw, height: (H - pad.b) - y0, rx: 4, fill: green, "fill-opacity": .92 }, svg);
            dEl("text", { x: cx, y: H - 8, class: "axis-label", "text-anchor": "middle" }, svg).textContent = "W" + (i + 1);
            if (v === mxv && v > 0) dEl("text", { x: cx, y: y0 - 5, class: "axis-label", "text-anchor": "middle", fill: dCv("--text-body") }, svg).textContent = v;
            const tip = ev => dShowTT(`<div class="tt-k">${currentLanguage === "he" ? "שבוע " + (i + 1) : "week " + (i + 1)}</div><div class="tt-v">${v} ${currentLanguage === "he" ? "דק' נחסכו" : "min saved"}</div>`, ev.clientX, ev.clientY);
            rr.addEventListener("mouseenter", tip); rr.addEventListener("mousemove", tip); rr.addEventListener("mouseleave", dHideTT);
        });
    }

    function dRenderCategories() {
        const host = document.getElementById("dash-cat-bars"); if (!host) return;
        const cats = dashAnalytics.by_category || [];
        if (!cats.length) { host.innerHTML = `<div style="color:var(--text-muted);font-size:12.5px">${currentLanguage === "he" ? "אין נתונים עדיין" : "No data yet"}</div>`; return; }
        const max = Math.max(...cats.map(c => c.v));
        host.innerHTML = cats.map(c => `<div class="hbar"><div class="hb-top"><span class="hb-name">${c.name}</span><span class="hb-val">${c.v}</span></div>
            <div class="hb-track"><div class="hb-fill" style="width:${Math.round(c.v / max * 100)}%"></div></div></div>`).join("");
    }

    function dRenderRisk() {
        const svg = document.getElementById("dash-risk-donut"); if (!svg) return; svg.innerHTML = "";
        const risk = dashAnalytics.by_risk || []; const total = risk.reduce((a, b) => a + b.v, 0);
        const legend = document.getElementById("dash-risk-legend");
        if (!total) { if (legend) legend.innerHTML = `<div style="color:var(--text-muted);font-size:12.5px">${currentLanguage === "he" ? "אין נתונים עדיין" : "No data yet"}</div>`; return; }
        const cx = 80, cy = 80, r = 58, th = 22, gap = .04; let ang = -Math.PI / 2;
        risk.forEach(s => {
            const col = dCv(s.c), frac = s.v / total, a0 = ang + gap / 2, a1 = ang + frac * 2 * Math.PI - gap / 2; ang += frac * 2 * Math.PI;
            const lg = (a1 - a0) > Math.PI ? 1 : 0, P = (aa, rr) => [cx + rr * Math.cos(aa), cy + rr * Math.sin(aa)];
            const [x0, y0] = P(a0, r), [x1, y1] = P(a1, r), [x2, y2] = P(a1, r - th), [x3, y3] = P(a0, r - th);
            const p = dEl("path", { d: `M${x0} ${y0} A${r} ${r} 0 ${lg} 1 ${x1} ${y1} L${x2} ${y2} A${r - th} ${r - th} 0 ${lg} 0 ${x3} ${y3} Z`, fill: col, "fill-opacity": .9 }, svg);
            const tip = ev => dShowTT(`<div class="tt-k">${s.name}</div><div class="tt-v">${s.v} · ${Math.round(frac * 100)}%</div>`, ev.clientX, ev.clientY);
            p.addEventListener("mouseenter", tip); p.addEventListener("mousemove", tip); p.addEventListener("mouseleave", dHideTT);
        });
        dEl("text", { x: cx, y: cy - 4, "text-anchor": "middle", fill: dCv("--text-main"), "font-size": 22, "font-weight": 800 }, svg).textContent = total;
        dEl("text", { x: cx, y: cy + 14, "text-anchor": "middle", fill: dCv("--text-muted"), "font-size": 10 }, svg).textContent = currentLanguage === "he" ? "סה\"כ הרצות" : "total runs";
        if (legend) legend.innerHTML = risk.map(s => `<div class="li"><span class="sw" style="background:${dCv(s.c)}"></span><span class="ln">${s.name}</span><span class="lv">${s.v} · ${Math.round(s.v / total * 100)}%</span></div>`).join("");
    }

    // Cumulative "time saved" - three squares (minutes / hours / days) shown
    // below the dashboard stat row. Fed by /api/stats/time-saved.
    function loadTimeSaved() {
        fetch("/api/stats/time-saved")
            .then(r => r.json())
            .then(d => { timeSaved = d; renderTimeSaved(); })
            .catch(() => {});
    }

    // Roll a minutes total up into whole days / hours / minutes components.
    function tsRollup(totalMin) {
        totalMin = Math.max(0, Math.round(totalMin));
        return {
            days: Math.floor(totalMin / 1440),
            hours: Math.floor((totalMin % 1440) / 60),
            mins: totalMin % 60,
        };
    }

    function renderTimeSaved() {
        const cards = document.getElementById("ts-cards");
        if (!cards) return;
        if (!timeSaved) { cards.innerHTML = ""; return; }
        const tr = t();
        const totalMin = timeSaved.minutes || 0;
        const runs = timeSaved.runs || 0;
        const servers = timeSaved.servers || 0;
        const bd = tsRollup(totalMin);

        // A labeled summary card (holds the raw totals) + the 3 breakdown squares,
        // as 4 equal cards so the row lines up with the KPI row above.
        const totalLine = currentLanguage === "he"
            ? `${totalMin.toLocaleString()} דק' · ${runs.toLocaleString()} הרצות · ${servers.toLocaleString()} שרתים`
            : `${totalMin.toLocaleString()} min · ${runs.toLocaleString()} runs · ${servers.toLocaleString()} servers`;
        const labelCard =
            `<div class="stat ts-label"><div class="accent"></div>` +
            `<div class="k">${tr.ts_title}</div>` +
            `<div class="ts-lead">${tr.ts_lead}</div>` +
            `<div class="s">${totalLine}</div></div>`;

        // label, then days -> hours -> minutes (largest to smallest)
        const units = [
            { k: tr.ts_unit_day, v: bd.days },
            { k: tr.ts_unit_hour, v: bd.hours },
            { k: tr.ts_unit_min, v: bd.mins },
        ];
        const unitCards = units.map(u =>
            `<div class="stat ts-card"><div class="accent"></div><div class="k">${u.k}</div><div class="v">${u.v.toLocaleString()}</div><div class="s"></div></div>`
        ).join("");
        cards.innerHTML = labelCard + unitCards;
    }

    function statCardHtml(k, v, s, cls, extra) {
        // cls (c1..c4) drives the per-category accent bar + number color via CSS,
        // matching the Liquid Glass preview palette.
        return `<div class="stat ${cls}"><div class="accent"></div>
            <div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div>${extra || ""}</div>`;
    }

    function handleScreenshotClear() {
        const tr = t();
        promptPassword(tr.ss_clear_title, tr.ss_clear_prompt, tr.ss_clear_placeholder).then(pw => {
            if (pw === null) return;  // cancelled
            fetch("/api/stats/screenshots/reset", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ password: pw })
            })
                .then(r => r.json().then(d => ({ status: r.status, d })))
                .then(({ status, d }) => {
                    if (status !== 200 || !d.ok) { showError(tr.ss_clear_wrong, ""); return; }
                    screenshotCount = 0;
                    renderDashboardStats();
                    showToast(tr.ss_clear_done, "success");
                })
                .catch(err => showError(tr.error_connection, err));
        });
    }

    function renderDashboardStats() {
        const container = document.getElementById("dash-stats");
        if (!container) return;
        const tr = t();
        const today = new Date().toISOString().slice(0, 10);
        const todays = historyData.filter(r => (r.date || "") === today);
        const finished = todays.filter(r => r.status !== "running");
        const success = finished.filter(r => r.status === "completed");
        const successRate = finished.length ? Math.round((success.length / finished.length) * 100) : 0;
        const runningRun = historyData.find(r => r.status === "running");
        const screenshotsDisplay = screenshotCount === null ? "..." : screenshotCount.toLocaleString();

        const clearBtn = `<button id="ss-clear-btn" class="btn btn-secondary btn-xs" style="position:absolute;top:10px;${currentLanguage === "he" ? "left" : "right"}:10px">${tr.ss_clear_btn}</button>`;

        container.innerHTML = [
            statCardHtml(tr.dash_stat_runs_today, todays.length, "", "c1"),
            statCardHtml(tr.dash_stat_success_rate, finished.length ? successRate + "%" : "-", finished.length ? `${success.length}/${finished.length} ${tr.dash_stat_succeeded}` : "", "c2"),
            statCardHtml(tr.dash_stat_screenshots, screenshotsDisplay, tr.dash_stat_screenshots_sub, "c3", clearBtn),
            statCardHtml(tr.dash_stat_running, runningRun ? 1 : 0, runningRun ? runningRun.script_name : "", "c4"),
        ].join("");

        const ssClear = document.getElementById("ss-clear-btn");
        if (ssClear) ssClear.onclick = handleScreenshotClear;

        renderRecentRunsTable();
    }

    function renderEnvHealthRows() {
        const container = document.getElementById("dash-env-rows");
        if (!container) return;
        const env = environmentInfo || {};
        const tr = t();
        const row = (name, val, ok) => `<div style="display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid var(--border-glass)">
            <span style="font-size:13px">${name}</span>
            <span style="display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:700;background:${ok ? "var(--accent-success-soft)" : "var(--accent-danger-soft)"};color:${ok ? "var(--accent-success)" : "var(--accent-danger)"}">${val}</span></div>`;
        container.innerHTML = [
            row("Google Chrome", env.chrome_version || tr.chrome_not_detected, !!env.chrome_version),
            row("ChromeDriver", env.has_matching_driver ? "✓" : "✗", !!env.has_matching_driver),
            row("racadm.exe", env.racadm_installed ? "✓" : "✗", !!env.racadm_installed),
            row("plink.exe", env.plink_installed ? "✓" : "✗", !!env.plink_installed),
        ].join("");
    }

    function renderRiskBreakdown() {
        const container = document.getElementById("dash-risk-rows");
        if (!container) return;
        const tr = t();
        const counts = { read: 0, config: 0, destructive: 0 };
        allScriptsData.forEach(s => { counts[s.risk || "read"] = (counts[s.risk || "read"] || 0) + 1; });
        const total = allScriptsData.length || 1;
        const bar = (label, risk, n) => {
            const pct = Math.round((n / total) * 100);
            const color = { read: "var(--accent-info)", config: "var(--accent-warning)", destructive: "var(--accent-danger)" }[risk];
            return `<div style="margin-bottom:10px"><div style="display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:4px">
                <span>${label}</span><span style="color:var(--text-muted)">${n} ${tr.dash_risk_count_suffix}</span></div>
                <div style="height:8px;background:var(--bg-input);border-radius:6px;overflow:hidden"><div style="width:${pct}%;height:100%;background:${color}"></div></div></div>`;
        };
        container.innerHTML =
            bar(riskLabel("read"), "read", counts.read) +
            bar(riskLabel("config"), "config", counts.config) +
            bar(riskLabel("destructive"), "destructive", counts.destructive) +
            `<p style="color:var(--text-muted);font-size:12px;margin-top:14px">${tr.dash_risk_caption}</p>`;
    }

    function renderRecentRunsTable() {
        const container = document.getElementById("dash-recent-runs");
        if (!container) return;
        const tr = t();
        const rows = historyData.slice(0, 5);
        if (!rows.length) { container.innerHTML = `<div class="list-placeholder">${tr.placeholder_history}</div>`; return; }
        let html = `<table class="reports-list-table"><thead><tr>
            <th>${tr.th_date}</th><th>${tr.th_automation}</th><th>${tr.dash_col_risk}</th><th>${tr.th_user}</th><th>${tr.th_servers}</th><th>${tr.th_status}</th><th>${tr.th_duration}</th>
        </tr></thead><tbody>`;
        rows.forEach(r => {
            const risk = riskByScriptName(r.script_name);
            const serverCount = (r.servers || []).length || (r.server_results || []).length || 0;
            html += `<tr><td class="mono">${r.date || ""} ${r.start_time || ""}</td><td><strong>${r.script_name || "-"}</strong></td>
                <td>${riskBadgeHtml(risk)}</td><td>${r.login_user || r.user || "-"}</td><td>${serverCount || "-"}</td>
                <td>${statusBadge(r.status)}</td><td class="mono">${r.duration || "-"}</td></tr>`;
        });
        html += "</tbody></table>";
        container.innerHTML = html;
    }

    // =====================================================================
    // Dynamic execution form builder
    // =====================================================================
    const eyeOpenSvg = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
    const eyeClosedSvg = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;

    function buildPasswordField(id, label, defaultValue) {
        const visible = prefs.show_password === true;
        return `
            <div class="form-group">
                <label for="${id}">${label} <span class="required">*</span></label>
                <div class="password-wrapper">
                    <input type="${visible ? "text" : "password"}" id="${id}" name="${id}" class="form-control" value="${defaultValue}" autocomplete="off">
                    <button type="button" class="password-toggle-btn" data-target="${id}" title="${visible ? t().password_hide_tip : t().password_show_tip}">
                        ${visible ? eyeClosedSvg : eyeOpenSvg}
                    </button>
                </div>
            </div>
        `;
    }

    function bindPasswordToggles(scope) {
        scope.querySelectorAll(".password-toggle-btn").forEach(btn => {
            btn.addEventListener("click", () => {
                const input = document.getElementById(btn.getAttribute("data-target"));
                if (!input) return;
                const nowVisible = input.type === "password";
                input.type = nowVisible ? "text" : "password";
                btn.innerHTML = nowVisible ? eyeClosedSvg : eyeOpenSvg;
                btn.title = nowVisible ? t().password_hide_tip : t().password_show_tip;
                savePref("show_password", nowVisible);
                prefs = loadPrefs();
            });
        });
    }

    // Default DNS/NTP servers pre-filled into the Configure_NTP+DNS form -
    // not secrets (just infrastructure IPs), so a plain constant is fine, no
    // encryption needed. Always fully editable: the operator can clear/replace
    // them before running, same as any other form field.
    const DEFAULT_DNS_SERVERS = "10.168.225.1\n10.168.225.2";
    const DEFAULT_NTP_SERVERS = "10.168.90.40\n10.169.201.1";

    function renderForm(script, prefill) {
        const form = document.getElementById("run-config-form");
        form.innerHTML = "";
        const tr = t();
        prefill = prefill || {};

        if (!script.inputs || !script.inputs.length) {
            form.innerHTML = `<div class="form-placeholder">${tr.form_no_inputs}</div>`;
            return;
        }

        if (script.inputs.includes("mode")) {
            const savedMode = prefill.mode || prefs.last_mode || "2";
            const group = document.createElement("div");
            group.className = "form-group";
            group.innerHTML = `
                <label>${tr.mode_label}</label>
                <div class="ip-mode-selector">
                    <div class="mode-option">
                        <input type="radio" id="mode-list-radio" name="mode" value="2" class="mode-radio" ${savedMode === "2" ? "checked" : ""}>
                        <label for="mode-list-radio" class="mode-label">${tr.mode_list}</label>
                    </div>
                    <div class="mode-option">
                        <input type="radio" id="mode-range-radio" name="mode" value="1" class="mode-radio" ${savedMode === "1" ? "checked" : ""}>
                        <label for="mode-range-radio" class="mode-label">${tr.mode_range}</label>
                    </div>
                </div>
            `;
            form.appendChild(group);

            group.querySelectorAll('input[name="mode"]').forEach(radio => {
                radio.addEventListener("change", (e) => {
                    savePref("last_mode", e.target.value);
                    toggleIpFormFields(e.target.value);
                });
            });
        }

        if (script.inputs.includes("base_ip")) {
            const ipRangeBlock = document.createElement("div");
            ipRangeBlock.id = "ip-range-inputs-block";
            ipRangeBlock.style.display = "none";
            ipRangeBlock.innerHTML = `
                <div class="form-group">
                    <label for="base_ip">${tr.base_ip} <span class="required">*</span></label>
                    <input type="text" id="base_ip" name="base_ip" class="form-control" placeholder="e.g., 192.168.0" value="${prefill.base_ip || prefs.last_base_ip || ""}">
                </div>
                <div class="form-group">
                    <label for="start_suffix">${tr.start_suffix} <span class="required">*</span></label>
                    <input type="number" id="start_suffix" name="start_suffix" class="form-control" value="${prefill.start_suffix || prefs.last_start_suffix || "207"}">
                </div>
                <div class="form-group">
                    <label for="count">${tr.count} <span class="required">*</span></label>
                    <input type="number" id="count" name="count" class="form-control" value="${prefill.count || prefs.last_count || "1"}">
                </div>
            `;
            form.appendChild(ipRangeBlock);
        }

        if (script.inputs.includes("ips")) {
            const ipListBlock = document.createElement("div");
            ipListBlock.id = "ip-list-inputs-block";
            ipListBlock.className = "form-group";
            // For Change_ip this "ips" list is the CURRENT iDRAC IPs; the label
            // gains a clarifying suffix only for that script.
            const ipsLabel = script.inputs.includes("newips") ? tr.ips_list_current : tr.ips_list;
            ipListBlock.innerHTML = `
                <label for="ips">${ipsLabel} <span class="required">*</span></label>
                <textarea id="ips" name="ips" class="form-control" placeholder="10.201.91.207&#10;10.201.91.208">${prefill.ips || prefs.last_ips || ""}</textarea>
                <div class="form-help">${tr.ips_range_hint}</div>
            `;
            form.appendChild(ipListBlock);
        }

        // Configure_Raid1: the RAID (virtual disk) name to create. Defaults to
        // "vDisk1" and is editable, but required - a RAID must have a name, so
        // an empty value is blocked (see the step-2 guard + server backstop).
        if (script.inputs.includes("raid_name")) {
            const raidBlock = document.createElement("div");
            raidBlock.className = "form-group";
            raidBlock.innerHTML = `
                <label for="raid_name">${tr.raid_name_label} <span class="required">*</span></label>
                <input type="text" id="raid_name" name="raid_name" class="form-control" placeholder="vDisk1" value="${prefill.raid_name || prefs.last_raid_name || "vDisk1"}">
                <div class="form-help">${tr.raid_name_hint}</div>
            `;
            form.appendChild(raidBlock);
        }

        // Change_ip: parallel NEW-IP list + shared netmask/gateway
        if (script.inputs.includes("newips")) {
            const newipsBlock = document.createElement("div");
            newipsBlock.id = "newips-inputs-block";
            newipsBlock.className = "form-group";
            newipsBlock.innerHTML = `
                <label for="newips">${tr.newips_list} <span class="required">*</span></label>
                <textarea id="newips" name="newips" class="form-control" placeholder="10.201.91.107&#10;10.201.91.108">${prefill.newips || prefs.last_newips || ""}</textarea>
                <div class="form-help">${tr.newips_range_hint}</div>
            `;
            form.appendChild(newipsBlock);
        }

        if (script.inputs.includes("netmask")) {
            const nmBlock = document.createElement("div");
            nmBlock.className = "form-group";
            nmBlock.innerHTML = `
                <label for="netmask">${tr.netmask_label}</label>
                <input type="text" id="netmask" name="netmask" class="form-control" placeholder="255.255.255.0" value="${prefill.netmask || prefs.last_netmask || ""}">
                <div class="form-help">${tr.netmask_hint}</div>
            `;
            form.appendChild(nmBlock);
        }

        if (script.inputs.includes("gateway")) {
            const gwBlock = document.createElement("div");
            gwBlock.className = "form-group";
            gwBlock.innerHTML = `
                <label for="gateway">${tr.gateway_label}</label>
                <input type="text" id="gateway" name="gateway" class="form-control" placeholder="10.201.91.254" value="${prefill.gateway || prefs.last_gateway || ""}">
                <div class="form-help">${tr.gateway_hint}</div>
            `;
            form.appendChild(gwBlock);
        }

        if (script.inputs.includes("hostnames")) {
            const hostnamesBlock = document.createElement("div");
            hostnamesBlock.id = "hostnames-inputs-block";
            hostnamesBlock.className = "form-group";
            hostnamesBlock.innerHTML = `
                <label for="hostnames">${tr.hostnames_list} <span class="required">*</span></label>
                <textarea id="hostnames" name="hostnames" class="form-control" placeholder="worker207&#10;worker208">${prefill.hostnames || prefs.last_hostnames || ""}</textarea>
                <div class="form-help">${tr.hostnames_range_hint}</div>
            `;
            form.appendChild(hostnamesBlock);
        }

        if (script.inputs.includes("dns")) {
            const dnsBlock = document.createElement("div");
            dnsBlock.id = "dns-inputs-block";
            dnsBlock.className = "form-group";
            dnsBlock.innerHTML = `
                <label for="dns">${tr.dns_list} <span class="required">*</span></label>
                <textarea id="dns" name="dns" class="form-control" placeholder="10.168.225.1&#10;10.168.225.2">${prefill.dns || prefs.last_dns || DEFAULT_DNS_SERVERS}</textarea>
            `;
            form.appendChild(dnsBlock);
        }

        if (script.inputs.includes("ntp")) {
            const ntpBlock = document.createElement("div");
            ntpBlock.id = "ntp-inputs-block";
            ntpBlock.className = "form-group";
            ntpBlock.innerHTML = `
                <label for="ntp">${tr.ntp_list} <span class="required">*</span></label>
                <textarea id="ntp" name="ntp" class="form-control" placeholder="10.168.90.40&#10;10.169.201.1">${prefill.ntp || prefs.last_ntp || DEFAULT_NTP_SERVERS}</textarea>
            `;
            form.appendChild(ntpBlock);
        }

        if (script.inputs.includes("use_default_creds")) {
            const savedCredsBool = prefill.use_default_creds !== undefined ? prefill.use_default_creds : (prefs.last_use_default_creds !== false);
            const savedCreds = savedCredsBool ? "yes" : "no";
            const defCredsGroup = document.createElement("div");
            defCredsGroup.className = "form-group";
            defCredsGroup.innerHTML = `
                <label>${tr.use_default_creds}</label>
                <div class="ip-mode-selector">
                    <div class="mode-option">
                        <input type="radio" id="creds-default-radio" name="use_default_creds" value="yes" class="mode-radio" ${savedCreds === "yes" ? "checked" : ""}>
                        <label for="creds-default-radio" class="mode-label">${tr.yes_y}</label>
                    </div>
                    <div class="mode-option">
                        <input type="radio" id="creds-custom-radio" name="use_default_creds" value="no" class="mode-radio" ${savedCreds === "no" ? "checked" : ""}>
                        <label for="creds-custom-radio" class="mode-label">${tr.no_n}</label>
                    </div>
                </div>
            `;
            form.appendChild(defCredsGroup);

            defCredsGroup.querySelectorAll('input[name="use_default_creds"]').forEach(radio => {
                radio.addEventListener("change", (e) => {
                    savePref("last_use_default_creds", e.target.value === "yes");
                    const customCredsBlock = document.getElementById("custom-credentials-block");
                    if (customCredsBlock) {
                        customCredsBlock.style.display = e.target.value === "no" ? "block" : "none";
                    }
                });
            });
        }

        if (script.inputs.includes("username") || script.inputs.includes("password")) {
            const credsBlock = document.createElement("div");
            credsBlock.id = "custom-credentials-block";
            if (script.inputs.includes("use_default_creds")) {
                const savedCredsBool = prefill.use_default_creds !== undefined ? prefill.use_default_creds : (prefs.last_use_default_creds !== false);
                credsBlock.style.display = savedCredsBool ? "none" : "block";
            }

            // SSH-authenticated scripts (plink) use a DIFFERENT default account
            // than iDRAC/racadm scripts - pick the matching default so the
            // pre-filled password is actually the one that will work.
            const isSsh = script.cred_kind === "ssh";
            const defaultUser = isSsh ? defaultCreds.ssh_username : defaultCreds.idrac_username;
            const defaultPass = isSsh ? defaultCreds.ssh_password : defaultCreds.idrac_password;

            let html = "";
            if (script.inputs.includes("username")) {
                html += `
                    <div class="form-group">
                        <label for="username">${tr.username} <span class="required">*</span></label>
                        <input type="text" id="username" name="username" class="form-control" value="${prefill.username || prefs.last_username || defaultUser || "root"}">
                    </div>
                `;
            }
            if (script.inputs.includes("password")) {
                html += buildPasswordField("password", tr.password, prefill.password || defaultPass || "");
            }
            credsBlock.innerHTML = html;
            form.appendChild(credsBlock);
            bindPasswordToggles(credsBlock);
        }

        // Chrome version - a per-script form parameter (addresses, username,
        // password, Chrome version), instead of a global sidebar control
        if (script.inputs.includes("chromedriver")) {
            form.appendChild(buildChromeDriverField(prefill.chromedriver_path));
        }

        if (script.inputs.includes("commands")) {
            const cmdBlock = document.createElement("div");
            cmdBlock.className = "form-group";

            let buttonsHtml = "";
            Object.keys(commandProfiles).forEach(key => {
                buttonsHtml += `<button type="button" class="template-btn" data-profile="${key}">${commandProfiles[key].label || key.toUpperCase()}</button>`;
            });
            buttonsHtml += `<button type="button" class="template-btn" data-profile="custom">${tr.custom_commands}</button>`;
            buttonsHtml += `<button type="button" class="template-btn template-save-btn" data-action="save-profile">${tr.save_profile_btn}</button>`;

            cmdBlock.innerHTML = `
                <div class="commands-templates-selector">
                    <label>${tr.server_type_label}</label>
                    <div class="template-btn-group">${buttonsHtml}</div>
                </div>
                <label for="commands">${tr.commands_label} <span class="required">*</span></label>
                <textarea id="commands" name="commands" class="form-control" style="height: 180px; font-family: monospace; font-size: 13px; text-align: left; direction: ltr;"></textarea>
            `;
            form.appendChild(cmdBlock);

            const textEditor = cmdBlock.querySelector("#commands");
            if (prefill.commands) {
                textEditor.value = prefill.commands;
                currentCommandProfile = "custom";
            } else {
                if (!commandProfiles[currentCommandProfile] && currentCommandProfile !== "custom") {
                    currentCommandProfile = Object.keys(commandProfiles)[0] || "custom";
                }
                textEditor.value = profileCommands(currentCommandProfile);
            }
            const activeBtn = cmdBlock.querySelector(`.template-btn[data-profile="${currentCommandProfile}"]`);
            if (activeBtn) activeBtn.classList.add("active");

            textEditor.addEventListener("input", () => {
                if (currentCommandProfile === "custom") {
                    savePref("last_custom_commands", textEditor.value);
                    prefs = loadPrefs();
                }
            });

            cmdBlock.querySelectorAll(".template-btn[data-profile]").forEach(btn => {
                btn.addEventListener("click", () => {
                    cmdBlock.querySelectorAll(".template-btn").forEach(b => b.classList.remove("active"));
                    btn.classList.add("active");
                    const profile = btn.getAttribute("data-profile");
                    currentCommandProfile = profile;
                    savePref("last_profile", profile);
                    prefs = loadPrefs();
                    textEditor.value = profileCommands(profile);
                });

                btn.addEventListener("dblclick", async () => {
                    const key = btn.getAttribute("data-profile");
                    if (key === "custom" || !commandProfiles[key] || commandProfiles[key].builtin) return;
                    const ok = await showConfirm(t().delete_profile_confirm + ` "${commandProfiles[key].label}"?`, "", t().btn_confirm, true);
                    if (!ok) return;
                    fetch(`/api/command_profiles/${key}`, { method: "DELETE" })
                        .then(r => r.json())
                        .then(d => {
                            if (d.error) { showError(d.error, ""); return; }
                            commandProfiles = d.profiles;
                            if (currentCommandProfile === key) currentCommandProfile = "custom";
                            renderForm(script);
                        })
                        .catch(err => showError("Delete profile", err));
                });
            });

            const saveBtn = cmdBlock.querySelector('[data-action="save-profile"]');
            saveBtn.addEventListener("click", () => {
                const name = window.prompt(tr.save_profile_prompt);
                if (!name || !name.trim()) return;
                fetch("/api/command_profiles", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ label: name.trim().toUpperCase(), commands: textEditor.value })
                })
                    .then(r => r.json())
                    .then(d => {
                        if (d.error) { showError(d.error, ""); return; }
                        commandProfiles = d.profiles;
                        currentCommandProfile = d.key;
                        savePref("last_profile", d.key);
                        prefs = loadPrefs();
                        showToast(tr.toast_profile_saved, "success");
                        renderForm(script);
                    })
                    .catch(err => showError("Save profile", err));
            });
        }

        form.addEventListener("change", () => {
            const ipsEl = document.getElementById("ips");
            if (ipsEl) savePref("last_ips", ipsEl.value);
            const hostnamesEl = document.getElementById("hostnames");
            if (hostnamesEl) savePref("last_hostnames", hostnamesEl.value);
            const dnsEl = document.getElementById("dns");
            if (dnsEl) savePref("last_dns", dnsEl.value);
            const ntpEl = document.getElementById("ntp");
            if (ntpEl) savePref("last_ntp", ntpEl.value);
            const userEl = document.getElementById("username");
            if (userEl) savePref("last_username", userEl.value);
            const baseIpEl = document.getElementById("base_ip");
            if (baseIpEl) savePref("last_base_ip", baseIpEl.value);
            const sufEl = document.getElementById("start_suffix");
            if (sufEl) savePref("last_start_suffix", sufEl.value);
            const cntEl = document.getElementById("count");
            if (cntEl) savePref("last_count", cntEl.value);
            prefs = loadPrefs();
        });

        const modeEl = form.querySelector('input[name="mode"]:checked');
        if (modeEl) toggleIpFormFields(modeEl.value);
    }

    function toggleIpFormFields(modeVal) {
        const rangeBlock = document.getElementById("ip-range-inputs-block");
        const listBlock = document.getElementById("ip-list-inputs-block");
        if (modeVal === "1") {
            if (rangeBlock) rangeBlock.style.display = "block";
            if (listBlock) listBlock.style.display = "none";
        } else {
            if (rangeBlock) rangeBlock.style.display = "none";
            if (listBlock) listBlock.style.display = "block";
        }
    }

    // =====================================================================
    // Scripts list (auto-detects scripts moved/added anywhere under Scripts/)
    // =====================================================================
    let scriptsListSignature = "";

    function riskLabel(risk) {
        const tr = t();
        return { read: tr.risk_read, config: tr.risk_config, destructive: tr.risk_destructive }[risk] || risk;
    }
    function riskBadgeHtml(risk) {
        return `<span class="risk risk-${risk}">${riskLabel(risk)}</span>`;
    }
    function riskIcon(script) {
        if (script.risk === "destructive") return "⏻";
        if (script.risk === "config") return "🔧";
        if ((script.inputs || []).includes("chromedriver")) return "🖥️";
        return "✅";
    }

    function loadAllScripts() {
        // Unscoped (no company/stage/type filter) - the full automations
        // catalog, used for the Dashboard risk breakdown and to look up a
        // run's risk category by name in the Audit page.
        return fetch("/api/scripts")
            .then(res => res.json())
            .then(data => { allScriptsData = Array.isArray(data) ? data : []; })
            .catch(() => {});
    }

    function riskByScriptName(name) {
        const found = allScriptsData.find(s => s.name === name);
        return found ? found.risk : "read";
    }

    // Catalog filters for Wizard step 1 (all client-side over the already
    // fetched list - no extra requests): free-text search + category (from the
    // sidebar). They combine with AND.
    let wizardSearchTerm = "";
    let wizardCategoryFilter = "all";

    // Functional categories, in the order they appear in the sidebar. Must
    // match the backend's classify_script_category() keys.
    const CATEGORY_ORDER = ["report", "configuration", "power", "storage", "network", "validation", "redhat_validation", "windows", "general"];

    function categoryLabel(key) {
        const tr = t();
        return (tr.categories && tr.categories[key]) ? tr.categories[key] : key;
    }

    function filteredScriptsForSearch() {
        let list = scriptsData;
        if (wizardCategoryFilter !== "all") {
            list = list.filter(s => (s.category || "general") === wizardCategoryFilter);
        }
        const q = wizardSearchTerm.trim().toLowerCase();
        if (q) {
            list = list.filter(s =>
                (s.name || "").toLowerCase().includes(q) ||
                (scriptDescription(s) || "").toLowerCase().includes(q)
            );
        }
        return list;
    }

    // The category navigator on the side of the catalog. Counts are the full
    // per-company totals per category (stable - not narrowed by the risk/search
    // filters), so the sidebar reads like a fixed table of contents.
    function renderCategorySidebar() {
        const el = document.getElementById("wiz-cat-list");
        if (!el) return;
        const tr = t();
        const counts = {};
        scriptsData.forEach(s => {
            const c = s.category || "general";
            counts[c] = (counts[c] || 0) + 1;
        });
        const cats = CATEGORY_ORDER.filter(c => counts[c]);
        const rowHtml = (key, label, count) => {
            const active = wizardCategoryFilter === key ? " active" : "";
            return `<button type="button" class="cat-row${active}" data-cat="${key}">
                        <span class="cat-count">${count}</span>
                        <span class="cat-name">${label}</span>
                    </button>`;
        };
        let html = rowHtml("all", tr.cat_all, scriptsData.length);
        cats.forEach(c => { html += rowHtml(c, categoryLabel(c), counts[c]); });
        el.innerHTML = html;
        el.querySelectorAll(".cat-row").forEach(row => {
            row.addEventListener("click", () => {
                wizardCategoryFilter = row.dataset.cat;
                renderCategorySidebar();
                renderWizardAutoList(filteredScriptsForSearch());
            });
        });
    }

    // Average REAL duration for a script, computed from completed runs in the
    // run history (never a made-up estimate). Returns null when there's no
    // completed run to average, so the card simply omits the duration.
    function scriptAvgDuration(script) {
        if (!Array.isArray(historyData) || !historyData.length) return null;
        const runs = historyData.filter(h =>
            h.script_name === script.name && h.status === "completed" && (h.duration_seconds || 0) > 0);
        if (!runs.length) return null;
        const avg = runs.reduce((a, h) => a + (h.duration_seconds || 0), 0) / runs.length;
        const tr = t();
        if (avg >= 60) return Math.round(avg / 60) + " " + tr.cat_minutes;
        return Math.round(avg) + " " + tr.cat_seconds;
    }

    const wizSearchInput = document.getElementById("wiz-search-input");
    if (wizSearchInput) {
        wizSearchInput.addEventListener("input", (e) => {
            wizardSearchTerm = e.target.value;
            renderWizardAutoList(filteredScriptsForSearch());
        });
    }

    function fetchScriptsList(forceRender) {
        if (!ctx.company) return;
        // No "type" filter - Python and PowerShell automations are listed
        // together; each item is tagged with its own language instead.
        const params = new URLSearchParams({ company: ctx.company });
        if (ctx.stage) params.set("stage", ctx.stage);

        fetch(`/api/scripts?${params.toString()}`)
            .then(res => res.json())
            .then(data => {
                const signature = JSON.stringify(data.map(s => s.id + "|" + s.name + "|" + s.category));
                if (forceRender || signature !== scriptsListSignature) {
                    scriptsListSignature = signature;
                    scriptsData = data;
                    renderCategorySidebar();
                    renderWizardAutoList(filteredScriptsForSearch());
                }
            })
            .catch(err => {
                console.error("Failed to load scripts list", err);
            });
    }

    setInterval(() => fetchScriptsList(false), 5000);

    function renderWizardAutoList(scripts) {
        const list = document.getElementById("wiz-auto-list");
        if (!list) return;
        list.innerHTML = "";
        const tr = t();

        if (selectedScript && !scripts.some(s => s.id === selectedScript.id)) {
            selectedScript = null;
            const nb = document.getElementById("wiz-step1-next");
            if (nb) nb.disabled = true;
        }

        if (!scripts.length) {
            list.innerHTML = `<div class="list-placeholder catalog-empty">${tr.no_automations_placeholder}</div>`;
            return;
        }

        // Python automations grouped together first, then PowerShell (stable
        // sort - relative order within each language is preserved).
        scripts = scripts.slice().sort((a, b) => {
            if (a.type === b.type) return 0;
            return a.type === "python" ? -1 : 1;
        });

        scripts.forEach(script => {
            const card = document.createElement("div");
            card.className = "cat-card";
            if (selectedScript && selectedScript.id === script.id) card.classList.add("sel");
            card.title = scriptDescription(script);
            const dur = scriptAvgDuration(script);
            card.innerHTML = `
                <div class="cc-top">
                    <div class="cc-ico risk-ico-${script.risk || "read"}">${riskIcon(script)}</div>
                    <button type="button" class="script-desc-edit-btn" title="${tr.edit_desc_tip}">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>
                        </svg>
                    </button>
                </div>
                <div class="cc-name"></div>
                <div class="cc-desc"></div>
                <div class="cc-foot">
                    <span class="tag">${script.type === "python" ? "Python" : "PowerShell"}</span>
                    ${riskBadgeHtml(script.risk || "read")}
                    ${dur ? `<span class="cc-dur">⏱ ${dur}</span>` : ""}
                </div>
            `;
            card.querySelector(".cc-name").textContent = script.name;
            card.querySelector(".cc-desc").textContent = scriptDescription(script);
            card.querySelector(".script-desc-edit-btn").addEventListener("click", (e) => {
                e.stopPropagation();
                openEditDescModal(script);
            });
            card.addEventListener("click", () => {
                document.querySelectorAll("#wiz-auto-list .cat-card").forEach(c => c.classList.remove("sel"));
                card.classList.add("sel");
                selectedScript = script;
                savePref("last_script", script.id);
                prefs = loadPrefs();
                document.getElementById("wiz-step1-next").disabled = false;
            });
            // Double-click = select (via the two preceding click events,
            // already handled above) AND immediately continue to step 2 -
            // same as a single click followed by pressing Continue.
            card.addEventListener("dblclick", () => {
                const nextBtn = document.getElementById("wiz-step1-next");
                if (nextBtn && !nextBtn.disabled) nextBtn.click();
            });
            list.appendChild(card);
        });

        if (!selectedScript && prefs.last_script) {
            const last = scripts.find(s => s.id === prefs.last_script);
            if (last) {
                const idx = scripts.indexOf(last);
                const el = list.children[idx];
                if (el) el.click();
            }
        }
    }

    // =====================================================================
    // Wizard: 4-step guided run flow (pick automation -> targets/inputs ->
    // pre-flight checks -> confirm & run), replacing the old always-visible
    // Dashboard workspace. All 4 steps drive the SAME real backend as before
    // (the dynamic form builder, /api/run, the SSE console) - only the
    // navigation/UX changed.
    // =====================================================================
    function renderWizardSteps() {
        const tr = t();
        const el = document.getElementById("wiz-steps");
        if (!el) return;
        const labels = [tr.wiz_step1_label, tr.wiz_step2_label, tr.wiz_step3_label, tr.wiz_step4_label];
        const curNum = WIZ.step === "live" ? 4 : WIZ.step;
        el.innerHTML = labels.map((lbl, i) => {
            const n = i + 1;
            const cls = curNum > n ? "done" : curNum === n ? "active" : "";
            return `<div class="step ${cls}"><div class="num">${curNum > n ? "✓" : n}</div><div class="lbl">${lbl}</div></div>`;
        }).join("");
    }

    function wizShowStep(view) {
        ["1", "2", "3", "4", "live"].forEach(v => {
            const el = document.getElementById(v === "live" ? "wiz-live" : "wiz-step-" + v);
            if (el) el.style.display = String(v) === String(view) ? "block" : "none";
        });
        if (view !== "live") { WIZ.step = view; renderWizardSteps(); }
    }

    function enterWizardPage() {
        if (activeRunId) { wizShowStep("live"); return; }
        if (WIZ.finished) {
            WIZ = { step: 1, finished: false, pendingTargetGroupId: WIZ.pendingTargetGroupId, schedulingMode: wizSchedulingIntent };
            wizSchedulingIntent = false;
        }
        wizShowStep(WIZ.step === "live" ? 1 : WIZ.step);
        if (WIZ.step === 1 || !WIZ.step) {
            renderCategorySidebar();
            renderWizardAutoList(filteredScriptsForSearch());
            // Pull run history so cards can show real average durations. When it
            // resolves, re-render the grid so the durations appear.
            if (!historyData.length) {
                loadReportsAndHistory();
                fetch("/api/history")
                    .then(r => r.json())
                    .then(h => {
                        historyData = Array.isArray(h) ? h : [];
                        if (currentPage === "wizard" && WIZ.step === 1) renderWizardAutoList(filteredScriptsForSearch());
                    })
                    .catch(() => {});
            }
        }
        if (WIZ.step === 2) wizRenderStep2();
        if (WIZ.step === 3) wizRenderStep3();
        if (WIZ.step === 4) wizRenderStep4();
    }

    document.getElementById("wiz-step1-next").addEventListener("click", () => {
        if (!selectedScript) return;
        wizShowStep(2);
        wizRenderStep2();
    });

    function applyTargetGroupToForm(group) {
        if (!group) return;
        const ipsEl = document.getElementById("ips");
        if (ipsEl) ipsEl.value = group.ips.join("\n");
        const modeListRadio = document.getElementById("mode-list-radio");
        if (modeListRadio) { modeListRadio.checked = true; toggleIpFormFields("2"); }
    }

    function wizRenderStep2(prefill) {
        const tr = t();
        document.getElementById("wiz-targets-script-name").textContent = selectedScript ? selectedScript.name : "";
        renderForm(selectedScript, prefill || {});

        const sourceGroup = document.getElementById("wiz-target-source-group");
        const groupPicker = document.getElementById("wiz-target-group-picker");
        const groupSelect = document.getElementById("wiz-target-group-select");
        const acceptsIps = selectedScript && (selectedScript.inputs || []).includes("ips");
        const relevantGroups = targetGroupsData.filter(g => !g.company || g.company === ctx.company || g.company === "");

        if (acceptsIps && relevantGroups.length) {
            sourceGroup.style.display = "block";
            groupSelect.innerHTML = relevantGroups.map(g => `<option value="${g.id}">${g.name} (${g.ips.length})</option>`).join("");
            document.getElementById("wiz-target-source").value = "manual";
            groupPicker.style.display = "none";

            if (WIZ.pendingTargetGroupId && relevantGroups.some(g => g.id === WIZ.pendingTargetGroupId)) {
                document.getElementById("wiz-target-source").value = "group";
                groupPicker.style.display = "block";
                groupSelect.value = WIZ.pendingTargetGroupId;
                applyTargetGroupToForm(relevantGroups.find(g => g.id === WIZ.pendingTargetGroupId));
                WIZ.pendingTargetGroupId = null;
            }
        } else {
            sourceGroup.style.display = "none";
            groupPicker.style.display = "none";
        }
    }

    document.getElementById("wiz-target-source").addEventListener("change", (e) => {
        const groupPicker = document.getElementById("wiz-target-group-picker");
        groupPicker.style.display = e.target.value === "group" ? "block" : "none";
        if (e.target.value === "group") {
            const groupSelect = document.getElementById("wiz-target-group-select");
            const group = targetGroupsData.find(g => g.id === groupSelect.value);
            applyTargetGroupToForm(group);
        }
    });
    document.getElementById("wiz-target-group-select").addEventListener("change", (e) => {
        applyTargetGroupToForm(targetGroupsData.find(g => g.id === e.target.value));
    });

    document.getElementById("wiz-step2-back").addEventListener("click", () => {
        wizShowStep(1);
        renderWizardAutoList(filteredScriptsForSearch());
    });

    document.getElementById("wiz-step2-next").addEventListener("click", () => {
        // A RAID must have a name: block advancing if the (required) RAID-name
        // field was cleared. The server + the script also reject an empty value.
        if (selectedScript && (selectedScript.inputs || []).includes("raid_name")) {
            const raidEl = document.getElementById("raid_name");
            if (raidEl && !raidEl.value.trim()) {
                showToast(t().raid_name_required, "warning");
                raidEl.focus();
                return;
            }
            if (raidEl) savePref("last_raid_name", raidEl.value.trim());
        }
        wizShowStep(3);
        wizRenderStep3();
    });

    function wizRenderStep3() {
        const tr = t();
        const payload = buildRunPayload();
        WIZ.lastPayload = payload;
        const ips = payloadIpList(payload);

        document.getElementById("wiz-checks").innerHTML = `
            <div class="check" id="chk-addr"><div class="ci"><span class="spin"></span></div>
                <div class="cbody"><div class="cname">${tr.wiz_check_addr}</div><div class="cdet" id="det-addr">${tr.wiz_checking}</div></div></div>
            <div class="check" id="chk-reach"><div class="ci">⏳</div>
                <div class="cbody"><div class="cname">${tr.wiz_check_reach}</div><div class="cdet" id="det-reach">${tr.wiz_pending}</div></div></div>
            <div class="check" id="chk-env"><div class="ci">⏳</div>
                <div class="cbody"><div class="cname">${tr.wiz_check_env}</div><div class="cdet" id="det-env">${tr.wiz_pending}</div></div></div>
        `;
        document.getElementById("wiz-preflight-result").innerHTML = "";
        document.getElementById("wiz-step3-next").disabled = true;

        const ipRe = /^\d{1,3}(\.\d{1,3}){3}$/;
        const validIps = ips.filter(ip => ipRe.test(ip));
        const invalidCount = ips.length - validIps.length;
        const uniqueIps = [...new Set(validIps)];
        const dupCount = validIps.length - uniqueIps.length;

        setTimeout(() => {
            const addrDet = ips.length
                ? `${uniqueIps.length} ${tr.wiz_addr_valid}${dupCount ? " · " + dupCount + " " + tr.wiz_addr_dupes : ""}${invalidCount ? " · " + invalidCount + " " + tr.wiz_addr_invalid : ""}`
                : tr.wiz_addr_none;
            document.getElementById("chk-addr").querySelector(".ci").innerHTML = (ips.length && !invalidCount) ? "✅" : (ips.length ? "⚠️" : "❌");
            document.getElementById("det-addr").textContent = addrDet;

            if (!uniqueIps.length) {
                document.getElementById("chk-reach").querySelector(".ci").innerHTML = "❌";
                document.getElementById("det-reach").textContent = tr.wiz_addr_none;
                document.getElementById("chk-env").querySelector(".ci").innerHTML = "❌";
                document.getElementById("det-env").textContent = tr.wiz_addr_none;
                document.getElementById("wiz-step3-next").disabled = false;
                return;
            }

            fetch("/api/preflight/reachability", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ ips: uniqueIps })
            })
                .then(r => r.json())
                .then(data => {
                    const reachable = data.reachable || 0;
                    const total = data.total || uniqueIps.length;
                    document.getElementById("chk-reach").querySelector(".ci").innerHTML = reachable === total ? "✅" : (reachable > 0 ? "⚠️" : "❌");
                    document.getElementById("det-reach").textContent = `${reachable}/${total} ${tr.wiz_reach_ok}`;

                    if (reachable < total) {
                        document.getElementById("wiz-preflight-result").innerHTML =
                            `<div class="callout callout-warn">⚠️ ${total - reachable} ${tr.wiz_reach_warn}</div>`;
                    }

                    // Environment check - real, based on what this script actually needs
                    const env = environmentInfo || {};
                    const needsChrome = (selectedScript.inputs || []).includes("chromedriver");
                    const envParts = [];
                    let envOk = true;
                    if (needsChrome) {
                        envParts.push(`ChromeDriver ${env.has_matching_driver ? "✓" : "✗"}`);
                        if (!env.has_matching_driver) envOk = false;
                    }
                    if (selectedScript.risk === "destructive" || (selectedScript.inputs || []).includes("username")) {
                        envParts.push(`racadm ${env.racadm_installed ? "✓" : "✗"}`);
                        if (!env.racadm_installed) envOk = false;
                    }
                    if (env.disk_free_gb != null) envParts.push(`${tr.dash_env_disk}: ${env.disk_free_gb} GB`);
                    document.getElementById("chk-env").querySelector(".ci").innerHTML = envOk ? "✅" : "⚠️";
                    document.getElementById("det-env").textContent = envParts.join(" · ") || tr.wiz_env_ok;

                    document.getElementById("wiz-step3-next").disabled = false;
                })
                .catch(() => {
                    document.getElementById("chk-reach").querySelector(".ci").innerHTML = "⚠️";
                    document.getElementById("det-reach").textContent = tr.wiz_reach_error;
                    document.getElementById("chk-env").querySelector(".ci").innerHTML = "⚠️";
                    document.getElementById("det-env").textContent = tr.wiz_env_ok;
                    document.getElementById("wiz-step3-next").disabled = false;
                });
        }, 400);
    }

    document.getElementById("wiz-step3-back").addEventListener("click", () => {
        wizShowStep(2);
        wizRenderStep2(WIZ.lastPayload);
    });

    document.getElementById("wiz-step3-next").addEventListener("click", () => {
        wizShowStep(4);
        wizRenderStep4();
    });

    function wizRenderStep4() {
        const tr = t();
        const payload = buildRunPayload();
        WIZ.lastPayload = payload;
        const serverCount = payloadServerCount(payload);
        const ips = payloadIpList(payload);
        const risk = selectedScript.risk || "read";
        const destructive = risk === "destructive";

        const previewPills = ips.slice(0, 4).map(ip =>
            `<span class="server-pill"><span class="dot" style="background:var(--accent-success)"></span>${ip}</span>`
        ).join("") + (ips.length > 4 ? `<span class="server-pill">+${ips.length - 4} ${tr.wiz_more}</span>` : "");

        document.getElementById("wiz-confirm-summary").innerHTML = `
            <div class="grid g2">
                <div><div class="card-t">${tr.summary_what_ran}</div><div style="font-weight:700">${riskIcon(selectedScript)} ${selectedScript.name}</div></div>
                <div><div class="card-t">${tr.dash_col_risk}</div>${riskBadgeHtml(risk)}</div>
                <div><div class="card-t">${tr.confirm_run_servers}</div><div style="font-weight:700">${serverCount}</div></div>
                <div><div class="card-t">${tr.summary_user}</div><div style="font-weight:700">${(environmentInfo.login_user || environmentInfo.user || "-")}</div></div>
            </div>
            <div style="margin-top:14px">${previewPills}</div>
        `;

        const gate = document.getElementById("wiz-destructive-gate");
        const runBtn = document.getElementById("wiz-final-run-btn");
        if (destructive) {
            gate.style.display = "block";
            gate.innerHTML = `<div class="callout callout-err">
                <div>⛔ <b>${tr.wiz_destructive_warning}</b><br>${tr.wiz_type_confirm}
                <div style="margin-top:10px;">
                    <input class="form-control" id="wiz-confirm-text" placeholder="${tr.wiz_confirm_placeholder}" style="max-width:220px;display:inline-block;">
                </div></div>
            </div>`;
            runBtn.disabled = true;
            runBtn.className = "btn btn-danger";
            document.getElementById("wiz-confirm-text").addEventListener("input", (e) => {
                runBtn.disabled = e.target.value.trim() !== "CONFIRM";
            });
        } else {
            gate.style.display = "none";
            gate.innerHTML = "";
            runBtn.disabled = false;
            runBtn.className = "btn btn-primary";
        }

        // Scheduling toggle: available on EVERY non-destructive run. OFF
        // (default) = the main button runs the automation immediately; ON =
        // the same button creates a schedule for the picked time instead.
        // Entering the Wizard from the Scheduling page's "+ New" button starts
        // with the toggle already ON. Destructive-risk automations can never
        // be scheduled - they must run interactively with the CONFIRM gate.
        const scheduleCard = document.getElementById("wiz-schedule-card");
        const blockedNote = document.getElementById("wiz-schedule-blocked-note");
        if (destructive) {
            wizScheduleOn = false;
            scheduleCard.style.display = "none";
            blockedNote.style.display = WIZ.schedulingMode ? "block" : "none";
            blockedNote.textContent = tr.wiz_schedule_blocked_destructive;
        } else {
            wizScheduleOn = !!WIZ.schedulingMode;
            scheduleCard.style.display = "block";
            blockedNote.style.display = "none";
        }
        updateWizScheduleUI();
    }

    // ---- Schedule on/off switch (Wizard step 4) ----
    let wizScheduleOn = false;

    function localDtValue(date) {
        return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    }

    function updateWizScheduleUI() {
        const tr = t();
        const toggle = document.getElementById("wiz-schedule-toggle");
        const dtGroup = document.getElementById("wiz-schedule-dt-group");
        const runLabel = document.getElementById("wiz-btn-run");
        if (!toggle || !dtGroup || !runLabel) return;
        toggle.classList.toggle("on", wizScheduleOn);
        toggle.setAttribute("aria-pressed", wizScheduleOn ? "true" : "false");
        dtGroup.style.display = wizScheduleOn ? "block" : "none";
        runLabel.textContent = wizScheduleOn ? ("🕒 " + tr.wiz_btn_schedule) : tr.wiz_btn_run;
        if (wizScheduleOn) {
            const dtInput = document.getElementById("wiz-schedule-dt");
            const nowVal = localDtValue(new Date());
            dtInput.min = nowVal;
            // Prefill/repair to one hour from now whenever empty or already in
            // the past, so "Scheduled time must be in the future" can't happen
            // just because the field kept a stale value.
            if (!dtInput.value || dtInput.value <= nowVal) {
                dtInput.value = localDtValue(new Date(Date.now() + 60 * 60 * 1000));
            }
        }
    }

    document.getElementById("wiz-schedule-toggle").addEventListener("click", () => {
        wizScheduleOn = !wizScheduleOn;
        updateWizScheduleUI();
    });

    document.getElementById("wiz-step4-back").addEventListener("click", () => {
        wizShowStep(3);
        wizRenderStep3();
    });

    document.getElementById("wiz-final-run-btn").addEventListener("click", () => {
        if (!selectedScript || activeRunId || runStarting) return;
        const tr = t();
        const payload = buildRunPayload();
        const runBtn = document.getElementById("wiz-final-run-btn");

        // Physically disable the button the instant it's clicked - a
        // disabled element can't dispatch further "click" events at all, so
        // a burst of rapid clicks (or an impatient double-click) collapses
        // to exactly one submission. This is on top of, not instead of, the
        // runStarting/activeRunId guard above and the server-side guard on
        // /api/run - belt and suspenders. Re-enabled below on every path
        // that doesn't move on to a live run (errors, or a created
        // schedule returning here) via wizRenderStep4(), which is also the
        // single source of truth for the destructive-action confirm gate.
        runBtn.disabled = true;

        if (wizScheduleOn) {
            const dtInput = document.getElementById("wiz-schedule-dt");
            if (!dtInput.value) { showToast(tr.wiz_schedule_pick_time, "warning"); runBtn.disabled = false; return; }
            fetch("/api/schedules", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ script_id: selectedScript.id, payload, scheduled_at: dtInput.value })
            })
                .then(res => res.json().then(data => ({ status: res.status, data })))
                .then(({ status, data }) => {
                    if (status !== 200 || data.error) { showError(tr.sched_create_failed, data.error || ""); wizRenderStep4(); return; }
                    showToast(tr.sched_created_toast, "success");
                    WIZ.finished = true;
                    switchPage("scheduling");
                })
                .catch(err => { showError(tr.error_connection, err); wizRenderStep4(); });
            return;
        }

        executeRun(payload);
    });

    // =====================================================================
    // Targets page - saved server groups (real CRUD against /api/targets),
    // reusable from the Wizard's step 2 so the operator doesn't retype IPs.
    // =====================================================================
    let editingTargetId = null;

    function loadTargetsPage() {
        return fetch("/api/targets")
            .then(r => r.json())
            .then(data => { targetGroupsData = Array.isArray(data) ? data : []; renderTargetsGrid(); })
            .catch(err => showError("Targets", err));
    }

    function renderTargetsGrid() {
        const tr = t();
        const grid = document.getElementById("targets-grid");
        if (!grid) return;
        if (!targetGroupsData.length) {
            grid.innerHTML = `<div class="list-placeholder">${tr.targets_empty}</div>`;
            return;
        }
        grid.innerHTML = targetGroupsData.map(g => `
            <div class="target-card" data-id="${g.id}">
                <div class="tc-head">
                    <div><div class="tc-name">${g.name}</div>${g.company ? `<span class="tag">${g.company}</span>` : ""}</div>
                    <span class="risk risk-read">${g.ips.length} ${tr.targets_servers}</span>
                </div>
                <div class="tc-ips">${g.ips.join("\n")}</div>
                <div class="tc-actions">
                    <button class="btn btn-sm btn-primary tc-run">▶ ${tr.targets_run}</button>
                    <button class="btn btn-sm tc-edit">${tr.targets_edit}</button>
                    <button class="btn btn-sm btn-danger tc-delete">${tr.targets_delete}</button>
                </div>
            </div>
        `).join("");

        grid.querySelectorAll(".target-card").forEach(card => {
            const id = card.getAttribute("data-id");
            const group = targetGroupsData.find(g => g.id === id);
            card.querySelector(".tc-run").addEventListener("click", () => {
                WIZ.pendingTargetGroupId = id;
                WIZ.finished = true;
                switchPage("wizard");
            });
            card.querySelector(".tc-edit").addEventListener("click", () => openTargetForm(group));
            card.querySelector(".tc-delete").addEventListener("click", async () => {
                const tr2 = t();
                const ok = await showConfirm(tr2.targets_delete_confirm + ` "${group.name}"?`, "", tr2.btn_confirm, true);
                if (!ok) return;
                fetch(`/api/targets/${id}`, { method: "DELETE" })
                    .then(r => r.json())
                    .then(d => { if (d.error) { showError(d.error, ""); return; } targetGroupsData = d.targets; renderTargetsGrid(); })
                    .catch(err => showError("Delete target", err));
            });
        });
    }

    function openTargetForm(group) {
        const tr = t();
        editingTargetId = group ? group.id : null;
        const card = document.getElementById("targets-form-card");
        card.style.display = "block";
        card.innerHTML = `
            <h3>${group ? tr.targets_edit : tr.targets_new}</h3>
            <div class="form-group"><label>${tr.targets_name_label}</label>
                <input type="text" id="tf-name" class="form-control" value="${group ? group.name : ""}"></div>
            <div class="form-group"><label>${tr.targets_ips_label}</label>
                <textarea id="tf-ips" class="form-control" placeholder="10.201.91.207&#10;10.201.91.208">${group ? group.ips.join("\n") : ""}</textarea></div>
            <div style="display:flex;gap:10px;">
                <button class="btn btn-primary" id="tf-save">${tr.btn_save}</button>
                <button class="btn" id="tf-cancel">${tr.btn_cancel}</button>
            </div>
        `;
        document.getElementById("tf-cancel").addEventListener("click", () => { card.style.display = "none"; card.innerHTML = ""; });
        document.getElementById("tf-save").addEventListener("click", () => {
            const name = document.getElementById("tf-name").value.trim();
            const ips = document.getElementById("tf-ips").value;
            if (!name || !ips.trim()) { showToast(tr.targets_validation, "warning"); return; }
            const body = JSON.stringify({ name, ips, company: ctx.company });
            const req = editingTargetId
                ? fetch(`/api/targets/${editingTargetId}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body })
                : fetch("/api/targets", { method: "POST", headers: { "Content-Type": "application/json" }, body });
            req.then(r => r.json())
                .then(d => {
                    if (d.error) { showError(d.error, ""); return; }
                    targetGroupsData = d.targets;
                    card.style.display = "none"; card.innerHTML = "";
                    renderTargetsGrid();
                })
                .catch(err => showError("Save target", err));
        });
    }

    document.getElementById("targets-new-btn").addEventListener("click", () => openTargetForm(null));

    // =====================================================================
    // Scheduling page - one-time scheduled runs (list/cancel/delete). Creating
    // a new schedule is done through the Wizard itself (same automation
    // picker + input form as a normal run) - this page's "+ New" button just
    // jumps into the Wizard flagged as WIZ.schedulingMode, and step 4 offers
    // "Schedule for later" instead of only "Run Now".
    // =====================================================================
    function loadSchedulingPage() {
        return fetch("/api/schedules")
            .then(r => r.json())
            .then(data => { schedulesData = Array.isArray(data) ? data : []; renderSchedulesTable(); })
            .catch(err => showError("Scheduling", err));
    }

    function schedStatusBadge(status) {
        const tr = t();
        const map = {
            pending: ["sched_status_pending", "var(--accent-info)"],
            triggered: ["sched_status_triggered", "var(--accent-success)"],
            cancelled: ["sched_status_cancelled", "var(--text-muted)"],
            failed: ["sched_status_failed", "var(--accent-danger)"]
        };
        const [key, color] = map[status] || map.pending;
        return `<span style="display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:700;color:${color};background:${color}22">${tr[key]}</span>`;
    }

    function renderSchedulesTable() {
        const tr = t();
        const container = document.getElementById("sched-list");
        if (!container) return;
        if (!schedulesData.length) {
            container.innerHTML = `<div class="list-placeholder">${tr.sched_empty}</div>`;
            return;
        }
        const rows = schedulesData.map(s => {
            const serverCount = payloadServerCount(s.payload || {});
            const isPending = s.status === "pending";
            return `
            <tr data-id="${s.id}">
                <td>${s.script_name}</td>
                <td>${serverCount}</td>
                <td class="sched-time-cell">${(s.scheduled_at || "").replace("T", " ")}</td>
                <td>${schedStatusBadge(s.status)}</td>
                <td>${s.created_by || "-"}</td>
                <td>
                    ${isPending ? `<button class="btn btn-sm sched-edit-btn">${tr.sched_edit_btn}</button>` : ""}
                    ${isPending ? `<button class="btn btn-sm sched-cancel-btn">${tr.sched_cancel_btn}</button>` : ""}
                    ${!isPending ? `<button class="btn btn-sm btn-danger sched-delete-btn">${tr.targets_delete}</button>` : ""}
                    ${s.error ? `<div class="form-help" style="color:var(--accent-danger)">${s.error}</div>` : ""}
                </td>
            </tr>`;
        }).join("");
        container.innerHTML = `
            <table class="reports-list-table sched-table">
                <thead><tr>
                    <th>${tr.sched_th_automation}</th>
                    <th>${tr.sched_th_servers}</th>
                    <th>${tr.sched_th_time}</th>
                    <th>${tr.sched_th_status}</th>
                    <th>${tr.sched_th_creator}</th>
                    <th>${tr.sched_th_actions}</th>
                </tr></thead>
                <tbody>${rows}</tbody>
            </table>`;

        container.querySelectorAll("tr[data-id]").forEach(row => {
            const id = row.getAttribute("data-id");
            const cancelBtn = row.querySelector(".sched-cancel-btn");
            const deleteBtn = row.querySelector(".sched-delete-btn");
            const editBtn = row.querySelector(".sched-edit-btn");
            if (editBtn) {
                editBtn.addEventListener("click", () => {
                    const tr2 = t();
                    const sched = schedulesData.find(s => s.id === id);
                    const cell = row.querySelector(".sched-time-cell");
                    if (!sched || !cell || cell.querySelector("input")) return;
                    const nowLocal = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
                    cell.innerHTML = `
                        <input type="datetime-local" class="form-control" style="max-width:190px;display:inline-block;padding:4px 8px;"
                               value="${sched.scheduled_at || ""}" min="${nowLocal}">
                        <button class="btn btn-sm btn-primary sched-save-btn">${tr2.btn_save}</button>
                        <button class="btn btn-sm sched-editcancel-btn">${tr2.btn_cancel}</button>`;
                    cell.querySelector(".sched-editcancel-btn").addEventListener("click", () => renderSchedulesTable());
                    cell.querySelector(".sched-save-btn").addEventListener("click", () => {
                        const val = cell.querySelector("input").value;
                        if (!val) { showToast(tr2.wiz_schedule_pick_time, "warning"); return; }
                        fetch(`/api/schedules/${id}`, {
                            method: "PUT",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ scheduled_at: val })
                        })
                            .then(r => r.json().then(d => ({ ok: r.ok, d })))
                            .then(({ ok, d }) => {
                                if (!ok || d.error) { showError(tr2.sched_edit_failed, d.error || ""); return; }
                                showToast(tr2.sched_updated_toast, "success");
                                loadSchedulingPage();
                            })
                            .catch(err => showError(tr2.error_connection, err));
                    });
                });
            }
            if (cancelBtn) {
                cancelBtn.addEventListener("click", async () => {
                    const tr2 = t();
                    const ok = await showConfirm(tr2.sched_cancel_confirm_title, "", tr2.btn_confirm, true);
                    if (!ok) return;
                    fetch(`/api/schedules/${id}/cancel`, { method: "POST" })
                        .then(r => r.json())
                        .then(d => { if (d.error) { showError(d.error, ""); return; } loadSchedulingPage(); })
                        .catch(err => showError("Cancel schedule", err));
                });
            }
            if (deleteBtn) {
                deleteBtn.addEventListener("click", async () => {
                    const tr2 = t();
                    const ok = await showConfirm(tr2.sched_delete_confirm_title, "", tr2.btn_confirm, true);
                    if (!ok) return;
                    fetch(`/api/schedules/${id}`, { method: "DELETE" })
                        .then(r => r.json())
                        .then(d => { if (d.error) { showError(d.error, ""); return; } schedulesData = d.schedules; renderSchedulesTable(); })
                        .catch(err => showError("Delete schedule", err));
                });
            }
        });
    }

    document.getElementById("sched-new-btn").addEventListener("click", () => {
        wizSchedulingIntent = true;
        WIZ.finished = true;
        switchPage("wizard");
    });

    // =====================================================================
    // Edit Description modal - lets you rewrite the description shown next
    // to any automation, in both languages, without touching its name
    // (the name always stays exactly the filename).
    // =====================================================================
    let editingScript = null;

    function openEditDescModal(script) {
        const tr = t();
        editingScript = script;
        document.getElementById("edit-desc-modal-title").textContent = `${tr.edit_desc_modal_title}: ${script.name}`;
        document.getElementById("edit-desc-he-label").textContent = tr.edit_desc_he_label;
        document.getElementById("edit-desc-en-label").textContent = tr.edit_desc_en_label;
        document.getElementById("edit-desc-he").value = script.description || "";
        document.getElementById("edit-desc-en").value = script.description_en || "";
        document.getElementById("edit-desc-save-btn").textContent = tr.btn_save;
        document.getElementById("edit-desc-cancel-btn").textContent = tr.btn_cancel;
        document.getElementById("edit-desc-translate-status").textContent = "";
        _lastEditedDescField = null;
        document.getElementById("edit-desc-modal").style.display = "flex";
    }

    // ------- Auto-translate between the two description fields -------
    // Typing in the English field fills the Hebrew one (and vice versa),
    // via the server's /api/translate. Last-edited field always wins.
    let _translateTimer = null;
    let _translateSeq = 0;
    let _lastEditedDescField = null;  // "he" or "en" - which side the user last typed in

    function _autoTranslateDesc(sourceId, targetId, targetLang) {
        const statusEl = document.getElementById("edit-desc-translate-status");
        clearTimeout(_translateTimer);
        _translateTimer = setTimeout(() => {
            const text = document.getElementById(sourceId).value.trim();
            const seq = ++_translateSeq;
            if (!text) {
                document.getElementById(targetId).value = "";
                statusEl.textContent = "";
                return;
            }
            statusEl.textContent = t().translate_working;
            fetch("/api/translate", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ text: text, target: targetLang })
            })
                .then(r => r.json())
                .then(d => {
                    if (seq !== _translateSeq) return;  // a newer edit superseded this one
                    if (d.translated) {
                        document.getElementById(targetId).value = d.translated;
                        statusEl.textContent = "";
                    } else {
                        statusEl.textContent = t().translate_offline;
                    }
                })
                .catch(() => { if (seq === _translateSeq) statusEl.textContent = t().translate_offline; });
        }, 700);
    }

    document.getElementById("edit-desc-he").addEventListener("input", () => {
        _lastEditedDescField = "he";
        _autoTranslateDesc("edit-desc-he", "edit-desc-en", "en");
    });
    document.getElementById("edit-desc-en").addEventListener("input", () => {
        _lastEditedDescField = "en";
        _autoTranslateDesc("edit-desc-en", "edit-desc-he", "he");
    });

    // Awaitable single translation - used by Save so a quick save still
    // captures the translation of the last-edited field even if the live
    // (debounced) translation hasn't landed yet.
    function _translateNow(text, target) {
        if (!text.trim()) return Promise.resolve("");
        return fetch("/api/translate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text: text.trim(), target: target })
        }).then(r => r.json()).then(d => d.translated || "").catch(() => "");
    }

    function closeEditDescModal() {
        document.getElementById("edit-desc-modal").style.display = "none";
        editingScript = null;
    }

    document.getElementById("edit-desc-modal-close").addEventListener("click", closeEditDescModal);
    document.getElementById("edit-desc-cancel-btn").addEventListener("click", closeEditDescModal);

    document.getElementById("edit-desc-save-btn").addEventListener("click", async () => {
        if (!editingScript) return;
        const tr = t();
        clearTimeout(_translateTimer);  // cancel any pending debounced translation
        const heEl = document.getElementById("edit-desc-he");
        const enEl = document.getElementById("edit-desc-en");
        const statusEl = document.getElementById("edit-desc-translate-status");
        const saveBtn = document.getElementById("edit-desc-save-btn");

        // Make the two languages consistent before saving: translate whichever
        // field was last edited into the other. This is the real fix for
        // "I changed the English and the Hebrew didn't change" - previously a
        // fast Save saved the OLD other-language value before the live
        // (debounced) translation had landed.
        saveBtn.disabled = true;
        try {
            if (_lastEditedDescField === "en" && enEl.value.trim()) {
                statusEl.textContent = tr.translate_working;
                const he = await _translateNow(enEl.value, "he");
                if (he) heEl.value = he;
            } else if (_lastEditedDescField === "he" && heEl.value.trim()) {
                statusEl.textContent = tr.translate_working;
                const en = await _translateNow(heEl.value, "en");
                if (en) enEl.value = en;
            }
            statusEl.textContent = "";
        } catch (e) {
            statusEl.textContent = "";  // offline - just save whatever we have
        }

        fetch("/api/scripts/description", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                filename: editingScript.filename,
                description: heEl.value.trim(),
                description_en: enEl.value.trim()
            })
        })
            .then(r => r.json())
            .then(d => {
                if (d.error) { showError(d.error, ""); return; }
                closeEditDescModal();
                showToast(tr.toast_desc_saved, "success");
                scriptsListSignature = "";
                fetchScriptsList(true);
            })
            .catch(err => showError("Edit description", err))
            .finally(() => { saveBtn.disabled = false; });
    });

    // =====================================================================
    // Add Automation modal - visual folder-tree destination picker
    // =====================================================================
    let destinationsData = [];
    let selectedDestination = null;

    function loadDestinationsTree() {
        return fetch("/api/scripts/destinations").then(r => r.json()).then(d => { destinationsData = d || []; });
    }

    function buildTreeLeaf(companyKey, stageKeyOrNull, lang) {
        const leaf = document.createElement("div");
        leaf.className = "tree-leaf";
        leaf.textContent = lang === "python" ? "Python" : "PowerShell";
        leaf.dataset.company = companyKey;
        leaf.dataset.stage = stageKeyOrNull || "";
        leaf.dataset.lang = lang;
        leaf.addEventListener("click", () => selectDestination(leaf));
        return leaf;
    }

    const caretSvg = `<svg class="tree-caret" viewBox="0 0 24 24" fill="currentColor"><polygon points="6 4 18 12 6 20"/></svg>`;

    function renderDestinationTree() {
        const container = document.getElementById("destination-tree");
        container.innerHTML = "";

        destinationsData.forEach(company => {
            const node = document.createElement("div");
            node.className = "tree-node";

            const label = document.createElement("div");
            label.className = "tree-node-label";
            label.innerHTML = caretSvg + `<span></span>`;
            label.querySelector("span").textContent = currentLanguage === "en" ? (company.label_en || company.label) : company.label;
            node.appendChild(label);

            const childrenWrap = document.createElement("div");
            childrenWrap.className = "tree-children";

            if (company.has_stages && (company.stages || []).length) {
                company.stages.forEach(stage => {
                    const stageNode = document.createElement("div");
                    stageNode.className = "tree-node collapsed";

                    const stageLbl = document.createElement("div");
                    stageLbl.className = "tree-node-label";
                    stageLbl.innerHTML = caretSvg + `<span></span>`;
                    stageLbl.querySelector("span").textContent = currentLanguage === "en" ? (stage.label_en || stage.label) : stage.label;
                    stageNode.appendChild(stageLbl);

                    const stageChildren = document.createElement("div");
                    stageChildren.className = "tree-children";
                    stageChildren.appendChild(buildTreeLeaf(company.key, stage.key, "powershell"));
                    stageChildren.appendChild(buildTreeLeaf(company.key, stage.key, "python"));
                    stageNode.appendChild(stageChildren);

                    stageLbl.addEventListener("click", () => stageNode.classList.toggle("collapsed"));
                    childrenWrap.appendChild(stageNode);
                });
            } else {
                childrenWrap.appendChild(buildTreeLeaf(company.key, null, "powershell"));
                childrenWrap.appendChild(buildTreeLeaf(company.key, null, "python"));
            }

            node.appendChild(childrenWrap);
            label.addEventListener("click", () => node.classList.toggle("collapsed"));
            container.appendChild(node);
        });
    }

    function findLeafElement(company, stage, lang) {
        return Array.from(document.querySelectorAll(".tree-leaf")).find(el =>
            el.dataset.company === company && (el.dataset.stage || "") === (stage || "") && el.dataset.lang === lang
        );
    }

    function selectDestination(leafEl) {
        document.querySelectorAll(".tree-leaf.selected").forEach(el => el.classList.remove("selected"));
        leafEl.classList.add("selected");
        // Expand ancestors so the selection is visible
        let p = leafEl.closest(".tree-node");
        while (p) {
            p.classList.remove("collapsed");
            p = p.parentElement ? p.parentElement.closest(".tree-node") : null;
        }
        selectedDestination = {
            company: leafEl.dataset.company,
            stage: leafEl.dataset.stage || null,
            type: leafEl.dataset.lang
        };
        updateDestPathLine();
        loadUnassignedFilesForDestination();
    }

    function updateDestPathLine() {
        const line = document.getElementById("add-automation-dest-line");
        if (!selectedDestination) {
            line.textContent = t().add_automation_no_dest;
            return;
        }
        const parts = ["Scripts", companyLabel(selectedDestination.company)];
        if (selectedDestination.stage) parts.push(stageLabel(selectedDestination.company, selectedDestination.stage));
        parts.push(selectedDestination.type === "python" ? "Python" : "PowerShell");
        line.textContent = parts.join(" / ") + " /";
    }

    function loadUnassignedFilesForDestination() {
        const tr = t();
        const fileSelect = document.getElementById("add-automation-file");
        const noFilesMsg = document.getElementById("add-automation-no-files");
        const saveBtn = document.getElementById("add-automation-save-btn");

        if (!selectedDestination) {
            fileSelect.innerHTML = "";
            saveBtn.disabled = true;
            return;
        }

        fetch(`/api/scripts/unassigned?type=${selectedDestination.type}`)
            .then(r => r.json())
            .then(files => {
                fileSelect.innerHTML = "";
                if (!files.length) {
                    noFilesMsg.textContent = tr.add_automation_no_files;
                    noFilesMsg.style.display = "block";
                    fileSelect.style.display = "none";
                    saveBtn.disabled = true;
                    return;
                }
                noFilesMsg.style.display = "none";
                fileSelect.style.display = "block";
                files.forEach(f => {
                    const opt = document.createElement("option");
                    opt.value = f.filename;
                    opt.textContent = f.filename;
                    fileSelect.appendChild(opt);
                });
                saveBtn.disabled = false;
                if (!document.getElementById("add-automation-name").value) {
                    document.getElementById("add-automation-name").value = files[0].suggested_name || "";
                }
            })
            .catch(err => showError("Unassigned scripts", err));
    }

    function openAddAutomationModal() {
        const tr = t();
        document.getElementById("add-automation-modal-title").textContent = tr.add_automation_modal_title;
        document.getElementById("add-automation-intro").textContent = tr.add_automation_intro;
        document.getElementById("add-automation-tree-label").textContent = tr.add_automation_tree_label;
        document.getElementById("add-automation-file-label").textContent = tr.add_automation_file_label;
        document.getElementById("add-automation-name-label").textContent = tr.add_automation_name_label;
        document.getElementById("add-automation-desc-he-label").textContent = tr.add_automation_desc_he_label;
        document.getElementById("add-automation-desc-en-label").textContent = tr.add_automation_desc_en_label;
        document.getElementById("add-automation-save-btn").textContent = tr.add_automation_save_btn;
        document.getElementById("add-automation-cancel-btn").textContent = tr.btn_cancel;

        document.getElementById("add-automation-name").value = "";
        document.getElementById("add-automation-desc-he").value = "";
        document.getElementById("add-automation-desc-en").value = "";
        selectedDestination = null;
        document.getElementById("add-automation-save-btn").disabled = true;

        loadDestinationsTree().then(() => {
            renderDestinationTree();
            updateDestPathLine();
            // Convenience: pre-select the current company/stage (defaulting to
            // the Python leaf) if it's a valid destination
            if (ctx.company && ctx.company !== "general") {
                const leaf = findLeafElement(ctx.company, ctx.stage || null, "python");
                if (leaf) selectDestination(leaf);
            }
        });

        document.getElementById("add-automation-modal").style.display = "flex";
    }

    document.getElementById("add-automation-btn").addEventListener("click", () => {
        if (!ctx.company) return;
        openAddAutomationModal();
    });

    function closeAddAutomationModal() {
        document.getElementById("add-automation-modal").style.display = "none";
    }
    document.getElementById("add-automation-modal-close").addEventListener("click", closeAddAutomationModal);
    document.getElementById("add-automation-cancel-btn").addEventListener("click", closeAddAutomationModal);

    document.getElementById("add-automation-save-btn").addEventListener("click", () => {
        const tr = t();
        const filename = document.getElementById("add-automation-file").value;
        if (!selectedDestination || !filename) return;

        fetch("/api/scripts/custom", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                name: document.getElementById("add-automation-name").value.trim(),
                description: document.getElementById("add-automation-desc-he").value.trim(),
                description_en: document.getElementById("add-automation-desc-en").value.trim(),
                filename: filename,
                company: selectedDestination.company,
                stage: selectedDestination.stage,
                type: selectedDestination.type
            })
        })
            .then(r => r.json())
            .then(d => {
                if (d.error) { showError(d.error, ""); return; }
                closeAddAutomationModal();
                showToast(tr.toast_automation_added, "success");
                scriptsListSignature = "";
                fetchScriptsList(true);
            })
            .catch(err => showError("Add automation", err));
    });

    // =====================================================================
    // History (full audit) + filters + export + Run Again
    // =====================================================================
    const historyFilters = { date: "", type: "", server: "", machineIp: "", status: "", search: "" };

    function renderHistoryFilters() {
        const tr = t();
        const wrap = document.getElementById("history-filters");
        if (!wrap) return;

        const types = [...new Set(historyData.map(h => h.script_name).filter(Boolean))];
        wrap.innerHTML = `
            <input type="date" id="hf-date" class="form-control" title="${tr.filter_date}" value="${historyFilters.date}">
            <select id="hf-type" class="form-control">
                <option value="">${tr.filter_type_all}</option>
                ${types.map(x => `<option value="${x}" ${historyFilters.type === x ? "selected" : ""}>${x}</option>`).join("")}
            </select>
            <input type="text" id="hf-server" class="form-control" placeholder="${tr.filter_server}" value="${historyFilters.server}">
            <input type="text" id="hf-machine-ip" class="form-control" placeholder="${tr.filter_machine_ip}" value="${historyFilters.machineIp}">
            <select id="hf-status" class="form-control">
                <option value="">${tr.filter_status_all}</option>
                <option value="completed" ${historyFilters.status === "completed" ? "selected" : ""}>${tr.status_completed_badge}</option>
                <option value="failed" ${historyFilters.status === "failed" ? "selected" : ""}>${tr.status_failed_badge}</option>
                <option value="killed" ${historyFilters.status === "killed" ? "selected" : ""}>${tr.status_killed_badge}</option>
                <option value="running" ${historyFilters.status === "running" ? "selected" : ""}>${tr.status_running_badge}</option>
            </select>
            <input type="text" id="hf-search" class="form-control" placeholder="${tr.filter_search}" value="${historyFilters.search}">
            <button id="hf-clear" class="btn btn-secondary btn-xs">${tr.filter_clear}</button>
        `;

        const bind = (id, key) => {
            const el = document.getElementById(id);
            el.addEventListener("input", () => {
                historyFilters[key] = el.value;
                renderHistoryTable();
            });
        };
        bind("hf-date", "date");
        bind("hf-type", "type");
        bind("hf-server", "server");
        bind("hf-machine-ip", "machineIp");
        bind("hf-status", "status");
        bind("hf-search", "search");
        document.getElementById("hf-clear").addEventListener("click", () => {
            historyFilters.date = historyFilters.type = historyFilters.server = historyFilters.machineIp = historyFilters.status = historyFilters.search = "";
            renderHistoryFilters();
            renderHistoryTable();
        });
    }

    function filteredHistory() {
        return historyData.filter(run => {
            if (historyFilters.date && (run.date || (run.started_at || "").substring(0, 10)) !== historyFilters.date) return false;
            if (historyFilters.type && run.script_name !== historyFilters.type) return false;
            if (historyFilters.server) {
                const servers = (run.servers || []).join(" ") + " " + (run.server_results || []).map(s => `${s.ip} ${s.hostname || ""}`).join(" ");
                if (!servers.toLowerCase().includes(historyFilters.server.toLowerCase())) return false;
            }
            if (historyFilters.machineIp && !(run.machine_ip || "").toLowerCase().includes(historyFilters.machineIp.toLowerCase())) return false;
            if (historyFilters.status && run.status !== historyFilters.status) return false;
            if (historyFilters.search) {
                const blob = JSON.stringify(run).toLowerCase();
                if (!blob.includes(historyFilters.search.toLowerCase())) return false;
            }
            return true;
        });
    }

    function statusBadge(status) {
        const tr = t();
        let cls = "badge-running", name = status;
        if (status === "completed") { cls = "badge-success"; name = tr.status_completed_badge; }
        else if (status === "failed") { cls = "badge-failed"; name = tr.status_failed_badge; }
        else if (status === "killed") { cls = "badge-killed"; name = tr.status_killed_badge; }
        else if (status === "running") { cls = "badge-running"; name = tr.status_running_badge; }
        return `<span class="badge ${cls}">${name}</span>`;
    }

    function renderHistoryTable() {
        const tr = t();
        const container = document.getElementById("history-container");
        const rows = filteredHistory();
        updateBulkDeleteBtn("logs");

        if (!rows.length) {
            container.innerHTML = `<div class="list-placeholder">${tr.placeholder_history}</div>`;
            return;
        }

        // Drop any remembered checks for runs no longer in the (filtered) list
        const currentRunIds = new Set(rows.map(r => r.run_id));
        checkedLogRunIds.forEach(id => { if (!currentRunIds.has(id)) checkedLogRunIds.delete(id); });

        let html = `
            <table class="reports-list-table">
                <thead>
                    <tr>
                        <th><input type="checkbox" id="history-select-all"></th>
                        <th>${tr.th_automation}</th>
                        <th>${tr.th_date}</th>
                        <th>${tr.th_start}</th>
                        <th>${tr.th_duration}</th>
                        <th>${tr.th_user}</th>
                        <th>${tr.th_machine_ip}</th>
                        <th>${tr.th_servers}</th>
                        <th>${tr.th_status}</th>
                        <th>${tr.th_fail_reason}</th>
                        <th>${tr.th_result}</th>
                        <th>${tr.th_actions}</th>
                    </tr>
                </thead>
                <tbody>
        `;

        rows.forEach((run, idx) => {
            // Logs page shows LOG information only: the per-run .log file
            // (with the full why-it-failed story). The generated .docx files
            // live exclusively on the Outputs page.
            const logPath = run.run_log_path || "";
            const logCell = logPath
                ? `<a class="report-item-link open-report-link" data-path="${logPath}">${logPath.split("\\").pop()}</a>`
                : "-";

            // Failure reason shown inline (truncated) + as a tooltip
            const reason = run.fail_reason || "";
            const reasonShort = reason.length > 60 ? reason.slice(0, 60) + "…" : reason;
            const statusCell = reason
                ? `<span title="${reason.replace(/"/g, "&quot;")}">${statusBadge(run.status)}</span>`
                : statusBadge(run.status);

            const serverCount = (run.servers || []).length || (run.server_results || []).length;

            const runChecked = checkedLogRunIds.has(run.run_id) ? "checked" : "";
            html += `
                <tr>
                    <td><input type="checkbox" class="row-check" data-run-id="${run.run_id || ""}" ${runChecked}></td>
                    <td><strong>${run.script_name || "-"}</strong></td>
                    <td>${run.date || (run.started_at || "").substring(0, 10)}</td>
                    <td>${run.start_time || (run.started_at || "").substring(11)}</td>
                    <td>${run.duration || "-"}</td>
                    <td>${run.user || "-"}</td>
                    <td>${run.machine_ip || "-"}</td>
                    <td>${serverCount || "-"}</td>
                    <td>${statusCell}</td>
                    <td class="fail-reason-cell" title="${reason.replace(/"/g, "&quot;")}">${reasonShort || "-"}</td>
                    <td>${logCell}</td>
                    <td>
                        <button class="btn btn-secondary btn-xs run-again-link" data-idx="${idx}">${tr.btn_run_again}</button>
                        <button class="btn btn-secondary btn-xs details-link" data-idx="${idx}">${tr.btn_details}</button>
                        ${logPath ? `<button class="btn btn-secondary btn-xs reveal-report-link" data-path="${logPath}">${tr.btn_reveal}</button>` : ""}
                        <button class="btn btn-danger btn-xs delete-log-link" data-run-id="${run.run_id || ""}">${tr.btn_delete}</button>
                    </td>
                </tr>
            `;
        });

        html += "</tbody></table>";
        container.innerHTML = html;

        container.querySelectorAll(".open-report-link").forEach(btn => {
            btn.addEventListener("click", () => openReportLocally(btn.getAttribute("data-path")));
        });
        container.querySelectorAll(".reveal-report-link").forEach(btn => {
            btn.addEventListener("click", () => revealReportLocally(btn.getAttribute("data-path")));
        });
        container.querySelectorAll(".details-link").forEach(btn => {
            btn.addEventListener("click", () => showRunDetails(rows[parseInt(btn.getAttribute("data-idx"), 10)]));
        });
        container.querySelectorAll(".run-again-link").forEach(btn => {
            btn.addEventListener("click", () => triggerRunAgain(rows[parseInt(btn.getAttribute("data-idx"), 10)]));
        });
        container.querySelectorAll(".delete-log-link").forEach(btn => {
            btn.addEventListener("click", () => confirmDeleteHistoryEntries([btn.getAttribute("data-run-id")]));
        });
        container.querySelectorAll(".row-check").forEach(cb => {
            cb.addEventListener("change", () => {
                const id = cb.getAttribute("data-run-id");
                if (cb.checked) checkedLogRunIds.add(id); else checkedLogRunIds.delete(id);
                updateBulkDeleteBtn("logs");
            });
        });
        const selectAllLogs = document.getElementById("history-select-all");
        if (selectAllLogs) {
            selectAllLogs.checked = rows.length > 0 && rows.every(r => checkedLogRunIds.has(r.run_id));
            selectAllLogs.addEventListener("change", () => {
                container.querySelectorAll(".row-check").forEach(cb => {
                    cb.checked = selectAllLogs.checked;
                    const id = cb.getAttribute("data-run-id");
                    if (selectAllLogs.checked) checkedLogRunIds.add(id); else checkedLogRunIds.delete(id);
                });
                updateBulkDeleteBtn("logs");
            });
        }
    }

    function triggerRunAgain(run) {
        // Takes the user STRAIGHT to Wizard step 2 (targets/inputs) with the
        // run's exact parameters pre-filled - skips step 1 (pick automation)
        // since it's already known. The user still reviews step 2-4 before
        // anything actually runs.
        const tr = t();
        if (!run.script_id || !run.params) {
            showToast(tr.run_again_unavailable, "warning");
            return;
        }

        const targetCtx = {
            company: run.company || "general",
            stage: run.stage || ""
        };
        const contextChanged = targetCtx.company !== ctx.company || targetCtx.stage !== ctx.stage;

        const applyPrefillAndSelect = () => {
            const script = scriptsData.find(s => s.id === run.script_id);
            if (!script) {
                showToast(tr.run_again_unavailable, "warning");
                return;
            }
            selectedScript = script;
            savePref("last_script", script.id);
            prefs = loadPrefs();
            WIZ.finished = false;
            wizShowStep(2);
            wizRenderStep2(run.params);
            showToast(tr.toast_run_again_loaded, "success");
        };

        if (contextChanged) {
            const companySelect = document.getElementById("wiz-company-select");
            if (companiesData[targetCtx.company]) {
                companySelect.value = targetCtx.company;
                ctx.company = targetCtx.company;
                ctx.stage = targetCtx.stage;
                renderWizardStageSelect();
                const stageSelect = document.getElementById("wiz-stage-select");
                if (stageSelect && targetCtx.stage) stageSelect.value = targetCtx.stage;
            }
            switchPage("wizard");
            setTimeout(applyPrefillAndSelect, 700); // allow the list to load
        } else {
            switchPage("wizard");
            applyPrefillAndSelect();
        }
    }

    function showRunDetails(run) {
        const tr = t();
        const overlay = document.getElementById("details-modal");
        document.getElementById("details-modal-title").textContent = `${tr.details_title}: ${run.script_name || "-"}`;
        const body = document.getElementById("details-modal-body");

        const serverResults = run.server_results || [];
        const outputs = run.outputs || [];

        body.innerHTML = `
            <div class="summary-section">
                <div class="summary-kv">
                    <span class="k">${tr.th_date}</span><span class="v">${run.date || "-"}</span>
                    <span class="k">${tr.summary_started}</span><span class="v">${run.start_time || "-"}</span>
                    <span class="k">${tr.details_end}</span><span class="v">${run.end_time || "-"}</span>
                    <span class="k">${tr.summary_duration}</span><span class="v">${run.duration || "-"}</span>
                    <span class="k">${tr.summary_user}</span><span class="v">${run.user || "-"}</span>
                    <span class="k">${tr.details_computer}</span><span class="v">${run.computer || "-"}</span>
                    <span class="k">${tr.th_machine_ip}</span><span class="v">${run.machine_ip || "-"}</span>
                    <span class="k">${tr.th_status}</span><span class="v">${statusBadge(run.status)}</span>
                    ${run.fail_reason ? `<span class="k">${tr.th_fail_reason}</span><span class="v" style="color:var(--accent-danger)">${run.fail_reason}</span>` : ""}
                </div>
            </div>
            <div class="summary-section">
                <h4>${tr.details_servers}</h4>
                ${(run.servers || []).length ? `<div class="modal-body-text">${(run.servers || []).join(", ")}</div>` : `<div class="modal-body-text">-</div>`}
            </div>
            ${serverResults.length ? `
            <div class="summary-section">
                <h4>${tr.summary_servers_section}</h4>
                <ul class="summary-list">
                    ${serverResults.map(s => `<li class="${s.status === "success" ? "ok" : "fail"}"><span>${s.ip} ${s.hostname ? "(" + s.hostname + ")" : ""} ${s.reason ? "- " + s.reason : ""}</span><span>${s.status === "success" ? "✔" : "✘"}</span></li>`).join("")}
                </ul>
            </div>` : ""}
            <div class="summary-section">
                <h4>${tr.details_outputs}</h4>
                ${outputs.length ? `
                <ul class="summary-list">
                    ${outputs.map(o => `<li class="ok"><span>${o.name}<br><small style="color:var(--text-muted)">${o.path}</small></span><button class="btn btn-secondary btn-xs details-open-file" data-path="${o.path}">${tr.btn_open}</button></li>`).join("")}
                </ul>` : `<div class="modal-body-text">${tr.summary_no_reports}</div>`}
            </div>
            ${run.run_log_path ? `
            <div class="summary-section">
                <button class="btn btn-secondary btn-sm details-open-file" data-path="${run.run_log_path}">${tr.btn_open_run_log}</button>
            </div>` : ""}
        `;

        body.querySelectorAll(".details-open-file").forEach(btn => {
            btn.addEventListener("click", () => openReportLocally(btn.getAttribute("data-path")));
        });

        overlay.style.display = "flex";
    }

    document.getElementById("details-modal-close").addEventListener("click", () => {
        document.getElementById("details-modal").style.display = "none";
    });

    document.getElementById("export-history-btn").addEventListener("click", () => {
        const tr = t();
        const rows = filteredHistory();
        if (!rows.length) {
            showToast(tr.placeholder_history, "warning");
            return;
        }
        const headers = [tr.th_automation, tr.th_date, tr.th_start, tr.details_end, tr.th_duration, tr.th_user, tr.th_machine_ip, tr.details_computer, tr.details_servers, tr.th_status, tr.th_fail_reason, tr.th_result, tr.details_outputs];
        const esc = (v) => `"${String(v === undefined || v === null ? "" : v).replace(/"/g, '""')}"`;
        const lines = [headers.map(esc).join(",")];
        rows.forEach(run => {
            lines.push([
                run.script_name || "",
                run.date || (run.started_at || "").substring(0, 10),
                run.start_time || "",
                run.end_time || "",
                run.duration || "",
                run.user || "",
                run.machine_ip || "",
                run.computer || "",
                (run.servers || []).join("; "),
                run.status || "",
                run.fail_reason || "",
                run.run_log_path || "",
                (run.outputs || []).map(o => o.path).join("; ") || run.path || ""
            ].map(esc).join(","));
        });
        const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8;" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "");
        a.download = `run_history_${stamp}.csv`;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
    });

    // =====================================================================
    // Reports & history loaders (Logs page + Output_Validation page)
    // =====================================================================
    function loadReportsAndHistory() {
        const tr = t();

        fetch("/api/history")
            .then(res => res.json())
            .then(history => {
                historyData = Array.isArray(history) ? history : [];
                if (currentPage === "logs") {
                    renderHistoryFilters();
                    renderHistoryTable();
                }
                if (currentPage === "dashboard") renderDashboardStats();
            })
            .catch(err => {
                const container = document.getElementById("history-container");
                if (container) container.innerHTML = `<div class="list-placeholder">${tr.error_prefix}: ${err}</div>`;
            });

        if (currentPage === "reports") {
            fetch("/api/reports")
                .then(res => res.json())
                .then(reports => {
                    const container = document.getElementById("reports-list");
                    container.innerHTML = "";
                    updateBulkDeleteBtn("reports");

                    if (!reports.length) {
                        container.innerHTML = `<div class="list-placeholder">${tr.placeholder_files}</div>`;
                        return;
                    }

                    // Drop any remembered checks for reports that no longer exist
                    const currentPaths = new Set(reports.map(r => r.path));
                    checkedReportPaths.forEach(p => { if (!currentPaths.has(p)) checkedReportPaths.delete(p); });

                    let html = `
                        <table class="reports-list-table">
                            <thead>
                                <tr>
                                    <th><input type="checkbox" id="reports-select-all"></th>
                                    <th>${tr.th_file_name}</th>
                                    <th>${tr.th_modified}</th>
                                    <th>${tr.th_size}</th>
                                    <th>${tr.th_actions}</th>
                                </tr>
                            </thead>
                            <tbody>
                    `;

                    reports.forEach(file => {
                        const sizeKB = (file.size / 1024).toFixed(1) + " KB";
                        const simpleName = file.name.split("/").pop();
                        const checkedAttr = checkedReportPaths.has(file.path) ? "checked" : "";
                        html += `
                            <tr>
                                <td><input type="checkbox" class="row-check" data-path="${file.path}" ${checkedAttr}></td>
                                <td><a class="report-item-link open-report-link" data-path="${file.path}"><strong>${simpleName}</strong></a></td>
                                <td>${file.modified}</td>
                                <td>${sizeKB}</td>
                                <td>
                                    <button class="btn btn-secondary btn-xs open-report-link" data-path="${file.path}">${tr.btn_open}</button>
                                    <button class="btn btn-secondary btn-xs reveal-report-link" data-path="${file.path}">${tr.btn_reveal}</button>
                                    <button class="btn btn-danger btn-xs delete-report-link" data-path="${file.path}">${tr.btn_delete}</button>
                                </td>
                            </tr>
                        `;
                    });

                    html += "</tbody></table>";
                    container.innerHTML = html;

                    container.querySelectorAll(".open-report-link").forEach(btn => {
                        btn.addEventListener("click", () => openReportLocally(btn.getAttribute("data-path")));
                    });
                    container.querySelectorAll(".reveal-report-link").forEach(btn => {
                        btn.addEventListener("click", () => revealReportLocally(btn.getAttribute("data-path")));
                    });
                    container.querySelectorAll(".delete-report-link").forEach(btn => {
                        btn.addEventListener("click", () => confirmDeleteReports([btn.getAttribute("data-path")]));
                    });
                    container.querySelectorAll(".row-check").forEach(cb => {
                        cb.addEventListener("change", () => {
                            const p = cb.getAttribute("data-path");
                            if (cb.checked) checkedReportPaths.add(p); else checkedReportPaths.delete(p);
                            updateBulkDeleteBtn("reports");
                        });
                    });
                    const selectAll = document.getElementById("reports-select-all");
                    if (selectAll) {
                        selectAll.checked = reports.length > 0 && reports.every(r => checkedReportPaths.has(r.path));
                        selectAll.addEventListener("change", () => {
                            container.querySelectorAll(".row-check").forEach(cb => {
                                cb.checked = selectAll.checked;
                                const p = cb.getAttribute("data-path");
                                if (selectAll.checked) checkedReportPaths.add(p); else checkedReportPaths.delete(p);
                            });
                            updateBulkDeleteBtn("reports");
                        });
                    }
                })
                .catch(err => {
                    document.getElementById("reports-list").innerHTML = `<div class="list-placeholder">${tr.error_prefix}: ${err}</div>`;
                });
        }
    }

    // ---------------------------------------------------------------------
    // Delete: Reports (Outputs files) + Logs (history entries) - both
    // require an explicit confirmation, and support selecting several rows
    // at once via the checkbox column + the "Delete Selected" button.
    // ---------------------------------------------------------------------
    function updateBulkDeleteBtn(kind) {
        const listSel = kind === "reports" ? "#reports-list .row-check:checked" : "#history-container .row-check:checked";
        const btn = document.getElementById(kind === "reports" ? "reports-delete-selected-btn" : "logs-delete-selected-btn");
        if (!btn) return;
        const count = document.querySelectorAll(listSel).length;
        btn.disabled = count === 0;
        const tr = t();
        const label = kind === "reports" ? tr.reports_delete_selected : tr.logs_delete_selected;
        btn.querySelector("span").textContent = count > 0 ? `${label} (${count})` : label;
    }

    function confirmDeleteReports(paths) {
        const tr = t();
        showConfirm(tr.delete_confirm_title, tr.delete_report_confirm_body.replace("{n}", paths.length), tr.btn_delete, true)
            .then(ok => {
                if (!ok) return;
                fetch("/api/reports/delete", {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ paths })
                })
                    .then(r => r.json())
                    .then(d => {
                        if (d.failed && d.failed.length) showError(tr.delete_partial_failed, d.failed.join(", "));
                        showToast(tr.toast_deleted, "success");
                        loadReportsAndHistory();
                    })
                    .catch(err => showError(tr.error_connection, err));
            });
    }

    function confirmDeleteHistoryEntries(runIds) {
        const tr = t();
        showConfirm(tr.delete_confirm_title, tr.delete_log_confirm_body.replace("{n}", runIds.length), tr.btn_delete, true)
            .then(ok => {
                if (!ok) return;
                fetch("/api/history/delete", {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ run_ids: runIds })
                })
                    .then(r => r.json())
                    .then(d => {
                        if (d.error) { showError(d.error, ""); return; }
                        showToast(tr.toast_deleted, "success");
                        loadReportsAndHistory();
                    })
                    .catch(err => showError(tr.error_connection, err));
            });
    }

    document.getElementById("reports-delete-selected-btn").addEventListener("click", () => {
        const paths = Array.from(document.querySelectorAll("#reports-list .row-check:checked")).map(cb => cb.getAttribute("data-path"));
        if (paths.length) confirmDeleteReports(paths);
    });
    document.getElementById("logs-delete-selected-btn").addEventListener("click", () => {
        const ids = Array.from(document.querySelectorAll("#history-container .row-check:checked")).map(cb => cb.getAttribute("data-run-id"));
        if (ids.length) confirmDeleteHistoryEntries(ids);
    });

    function openReportLocally(path) {
        fetch("/api/reports/open", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: path })
        })
            .then(res => res.json())
            .then(data => {
                if (data.error) showError(data.error, path);
                else showToast(data.message || t().toast_file_opened);
            })
            .catch(err => showError(t().error_connection, err));
    }

    function revealReportLocally(path) {
        fetch("/api/reports/reveal", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: path })
        })
            .then(res => res.json())
            .then(data => {
                if (data.error) showError(data.error, path);
            })
            .catch(err => showError(t().error_connection, err));
    }

    // Open the project's Outputs folder and highlight the exact output the
    // just-finished run produced (walks up to the run item under Outputs).
    function revealRunOutput(path) {
        fetch("/api/reports/reveal-run", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: path })
        })
            .then(res => res.json())
            .then(data => {
                if (data.error) showError(data.error, path);
                else showToast(t().toast_output_revealed || t().toast_file_opened);
            })
            .catch(err => showError(t().error_connection, err));
    }

    document.getElementById("refresh-history-btn").addEventListener("click", () => {
        loadReportsAndHistory();
        showToast(t().toast_reports_refreshed);
    });

    document.getElementById("refresh-files-btn").addEventListener("click", () => {
        loadReportsAndHistory();
        showToast(t().toast_reports_refreshed);
    });

    document.getElementById("open-reports-folder-btn").addEventListener("click", () => {
        openReportLocally("RESULTS_DIR");   // -> Output_Validation folder
    });

    document.getElementById("open-report-folder-btn").addEventListener("click", () => {
        openReportLocally("REPORT_FOLDER_DIR");   // -> Logs folder
    });

    document.getElementById("open-scripts-folder-btn").addEventListener("click", () => {
        openReportLocally("SCRIPTS_DIR");
    });

    document.getElementById("clear-console-btn").addEventListener("click", () => {
        document.getElementById("console-terminal").innerHTML = "";
        showToast(t().toast_console_cleared);
    });

    // =====================================================================
    // Live progress bar, current stage & per-server status chips
    // =====================================================================
    function resetProgressUI() {
        activeServerFilter = null;   // start each run showing the full merged log
        document.getElementById("progress-area").style.display = "none";
        document.getElementById("server-status-panel").style.display = "none";
        document.getElementById("server-status-panel").innerHTML = "";
        const fill = document.getElementById("progress-bar-fill");
        fill.style.width = "0%";
        fill.className = "progress-bar-fill";
    }

    function updateProgressUI(status) {
        const tr = t();
        const area = document.getElementById("progress-area");
        const fill = document.getElementById("progress-bar-fill");
        const stageEl = document.getElementById("progress-stage");
        const countEl = document.getElementById("progress-count");

        area.style.display = "block";

        const done = status.progress_done;
        const total = status.progress_total;

        if (total && total > 0 && done !== null && done !== undefined) {
            const pct = Math.min(100, Math.round((done / total) * 100));
            fill.classList.remove("indeterminate");
            fill.style.width = pct + "%";
            countEl.textContent = `${done} ${tr.progress_of} ${total} (${pct}%)`;
        } else {
            fill.classList.add("indeterminate");
            countEl.textContent = "";
        }

        stageEl.textContent = status.current_stage || (status.status === "running" ? tr.stage_waiting : "");

        if (status.status !== "running") {
            fill.classList.remove("indeterminate");
            if (total && total > 0 && done !== null && done !== undefined) {
                fill.style.width = Math.min(100, Math.round((done / total) * 100)) + "%";
            } else {
                fill.style.width = "100%";
            }
            fill.classList.add(status.status === "completed" ? "done-success" : "done-failed");
        }

        const panel = document.getElementById("server-status-panel");
        const servers = status.servers || [];
        const targets = status.target_servers || [];

        if (servers.length || targets.length > 1) {
            panel.style.display = "flex";
            const byIp = {};
            servers.forEach(s => { byIp[s.ip] = s; });
            const allIps = [...new Set([...targets, ...servers.map(s => s.ip)])];
            // "All" chip (merged log) first, then one per server. Clicking a
            // server chip filters the console to just that server's session.
            let html = `<span class="server-chip chip-all ${activeServerFilter ? "" : "chip-active"}" data-ip=""><span class="chip-dot"></span>${tr.chip_all_servers || "All"}</span>`;
            html += allIps.map(ip => {
                const s = byIp[ip];
                const state = s ? s.status : "pending";
                const label = s && s.hostname ? `${s.hostname} (${ip})` : ip;
                const title = s && s.reason ? s.reason : "";
                const activeCls = activeServerFilter === ip ? "chip-active" : "";
                return `<span class="server-chip ${state} ${activeCls}" data-ip="${ip}" title="${title}"><span class="chip-dot"></span>${label}</span>`;
            }).join("");
            panel.innerHTML = html;
            panel.querySelectorAll(".server-chip").forEach(chip => {
                chip.style.cursor = "pointer";
                chip.onclick = () => applyServerFilter(chip.dataset.ip || null);
            });
        }

        const liveStats = document.getElementById("wiz-live-stats");
        if (liveStats) {
            const doneCount = servers.filter(s => s.status === "success").length;
            const failCount = servers.filter(s => s.status && s.status !== "success" && s.status !== "pending").length;
            const statusLabel = status.status === "running" ? tr.status_running_badge
                : status.status === "completed" ? tr.status_completed_badge
                : status.status === "failed" ? tr.status_failed_badge
                : tr.status_killed_badge;
            liveStats.innerHTML = [
                statCardHtml(tr.wiz_live_total, total || (targets.length || "-"), "", "var(--accent-info)"),
                statCardHtml(tr.wiz_live_done, doneCount, "", "var(--accent-success)"),
                statCardHtml(tr.wiz_live_failed, failCount, "", "var(--accent-danger)"),
                statCardHtml(tr.wiz_live_status, statusLabel, "", "var(--accent-primary)"),
            ].join("");
        }

        // Keep the floating job window in sync with the same live status.
        traySyncStatus(status);
    }

    // =====================================================================
    // IP / hostname RANGE expansion - an ADDITIONAL way to write the "ips" and
    // "hostnames" list fields, on top of (never replacing) typing one value
    // per line as before. A line like "192.168.0.1-192.168.0.3" or
    // "kafka1-kafka3" is expanded into individual lines right before the run
    // payload is sent - every automation script still only ever receives a
    // plain, fully-expanded list (addresses.txt/hostnames.txt), exactly as
    // today, so nothing about how scripts read their targets needs to change.
    // Any line that isn't a recognized range is passed through completely
    // unchanged, so a single plain IP/hostname per line keeps working exactly
    // as before.
    function expandIpRangeLine(line) {
        const m = line.match(/^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s*-\s*(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})$/);
        if (!m) return null;
        const startParts = m[1].split(".").map(Number);
        const endParts = m[2].split(".").map(Number);
        if (startParts.some(p => p > 255) || endParts.some(p => p > 255)) return null;
        // Only the last octet may differ - matches how every IP list in this
        // app is scoped (a single /24 of target servers).
        if (startParts[0] !== endParts[0] || startParts[1] !== endParts[1] || startParts[2] !== endParts[2]) return null;
        const start = startParts[3], end = endParts[3];
        if (start > end) return null;
        const prefix = startParts.slice(0, 3).join(".");
        const result = [];
        for (let i = start; i <= end; i++) result.push(`${prefix}.${i}`);
        return result;
    }

    function expandHostnameRangeLine(line) {
        const m = line.match(/^([A-Za-z0-9_.-]*?)(\d+)\s*-\s*([A-Za-z0-9_.-]*?)(\d+)$/);
        if (!m) return null;
        const [, prefix1, numStr1, prefix2, numStr2] = m;
        if (prefix1 !== prefix2) return null;   // both sides must share the same name prefix
        const start = parseInt(numStr1, 10), end = parseInt(numStr2, 10);
        if (isNaN(start) || isNaN(end) || start > end) return null;
        const width = numStr1.length;           // preserve zero-padding, e.g. kafka01-kafka03
        const result = [];
        for (let i = start; i <= end; i++) result.push(prefix1 + String(i).padStart(width, "0"));
        return result;
    }

    function expandRangeLines(text, kind) {
        return text.split("\n").map(raw => {
            const line = raw.trim();
            if (!line) return raw;
            // Try the IP-specific expander first even for the "hostnames"
            // field (its stricter octet-range validation is the more correct
            // match whenever the line actually looks like two dotted IPs),
            // falling back to the general prefix+number expander otherwise.
            const expanded = expandIpRangeLine(line) || (kind === "hostnames" ? expandHostnameRangeLine(line) : null);
            return expanded ? expanded.join("\n") : line;
        }).join("\n");
    }

    // =====================================================================
    // Run / kill execution flow - triggered from the Wizard's step 4 (Confirm
    // & run), not from a standalone button, since step 4 IS the confirmation.
    // =====================================================================
    const killBtn = document.getElementById("kill-btn");

    function buildRunPayload() {
        const formData = new FormData(document.getElementById("run-config-form"));
        const payload = { script_id: selectedScript.id };
        for (const [key, value] of formData.entries()) {
            if (key === "use_default_creds") payload[key] = (value === "yes");
            else if (key === "ips" || key === "hostnames" || key === "newips") payload[key] = expandRangeLines(value, key === "newips" ? "ips" : key);
            else payload[key] = value;
        }
        return payload;
    }

    function payloadServerCount(payload) {
        if (payload.mode === "1") return parseInt(payload.count || "1", 10) || 1;
        if (payload.ips) return payload.ips.split("\n").filter(l => l.trim()).length;
        return 0;
    }

    function payloadIpList(payload) {
        if (payload.mode === "1" && payload.base_ip) {
            const start = parseInt(payload.start_suffix || "1", 10) || 1;
            const count = parseInt(payload.count || "1", 10) || 1;
            return Array.from({ length: count }, (_, i) => `${payload.base_ip}.${start + i}`);
        }
        return (payload.ips || "").split("\n").map(s => s.trim()).filter(Boolean);
    }

    // =====================================================================
    // Live job window (Jenkins-style): a single floating card showing the
    // currently running automation. Click its title to jump into the live run
    // screen; the X stops it while running, or closes the card once finished.
    // It reflects the SAME poll/stream the live screen uses (no extra polling),
    // so it stays live on whatever page you're on. Docked inline-start, it sits
    // LEFT in English (LTR) and RIGHT in Hebrew (RTL) automatically.
    // (trayJob state is declared near the top with the other run-state vars.)
    // =====================================================================
    const JT_SVG = {
        check: '<svg viewBox="0 0 24 24" fill="none" stroke="var(--accent-success)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>',
        cross: '<svg viewBox="0 0 24 24" fill="none" stroke="var(--accent-danger)" stroke-width="3" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>',
        stop:  '<svg viewBox="0 0 24 24" fill="var(--accent-warning)"><rect x="7" y="7" width="10" height="10" rx="2"/></svg>',
        x:     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>',
    };

    function trayStart(runId, name, total) {
        trayJob = { runId: runId, name: name || "", status: "running", servers: [], targets: [],
                    total: total || 0, startTs: Date.now(), endTs: 0, finalStatus: null };
        renderJobTray();
    }

    // Fed by the same /api/status poll that drives the live screen.
    function traySyncStatus(status) {
        if (!trayJob || !status) return;
        if (status.run_id && trayJob.runId && status.run_id !== trayJob.runId) return;
        trayJob.servers = status.servers || [];
        trayJob.targets = status.target_servers || [];
        if (status.script_name) trayJob.name = status.script_name;
        if (typeof status.progress_total === "number" && status.progress_total) trayJob.total = status.progress_total;
        if (trayJob.status === "running") renderJobTray();
    }

    function trayFinish(runId, finalStatus) {
        if (!trayJob || trayJob.runId !== runId) return;
        trayJob.status = (finalStatus && finalStatus.status) || "failed";
        trayJob.finalStatus = finalStatus || null;
        trayJob.endTs = Date.now();
        if (finalStatus && finalStatus.servers) trayJob.servers = finalStatus.servers;
        if (finalStatus && finalStatus.target_servers) trayJob.targets = finalStatus.target_servers;
        renderJobTray();
    }

    function trayDismiss() {
        const host = document.getElementById("job-tray");
        if (!host) { trayJob = null; return; }
        host.classList.add("is-removing");
        setTimeout(() => { host.classList.remove("is-removing"); trayJob = null; renderJobTray(); }, 240);
    }

    // X: running -> stop the job (same as the live-screen Kill button);
    //    finished -> just close the little window.
    function trayX() {
        if (!trayJob) return;
        if (trayJob.status === "running") {
            const tr = t();
            const runId = trayJob.runId;
            showConfirm(tr.confirm_kill_title, tr.confirm_kill_body, tr.btn_confirm_kill, true).then(ok => {
                if (!ok) return;
                fetch(`/api/kill/${runId}`, { method: "POST" })
                    .then(res => res.json())
                    .then(data => {
                        if (data.error) showError(data.error, "");
                        else {
                            showToast(tr.toast_killed, "warning");
                            if (["logs", "dashboard"].includes(currentPage)) loadReportsAndHistory();
                        }
                    })
                    .catch(err => showError(tr.error_connection, err));
            });
        } else {
            trayDismiss();
        }
    }

    // Click the title: running -> jump into the live run screen (exactly the
    // screen you get when running the automation directly); finished -> re-open
    // its run summary.
    function trayOpen() {
        if (!trayJob) return;
        if (trayJob.status === "running") {
            switchPage("wizard");   // enterWizardPage() shows the live view while activeRunId is set
        } else if (trayJob.finalStatus) {
            showRunSummary(trayJob.finalStatus);
        }
    }

    function jtElapsed() {
        const end = trayJob.endTs || Date.now();
        const s = Math.max(0, Math.floor((end - trayJob.startTs) / 1000));
        return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
    }

    function renderJobTray() {
        const host = document.getElementById("job-tray");
        if (!host) return;
        if (!trayJob) { host.style.display = "none"; host.innerHTML = ""; return; }
        const tr = t();
        host.style.display = "block";

        const byIp = {};
        (trayJob.servers || []).forEach(s => { byIp[s.ip] = s; });
        const allIps = [...new Set([...(trayJob.targets || []), ...(trayJob.servers || []).map(s => s.ip)])];

        const okC = (trayJob.servers || []).filter(s => s.status === "success").length;
        const badC = (trayJob.servers || []).filter(s => s.status === "failed").length;
        const doneC = okC + badC;
        const totalC = trayJob.total || allIps.length || 0;
        const pct = totalC ? Math.round((doneC / totalC) * 100) : (trayJob.status === "running" ? 0 : 100);

        const stateLabel = { pending: tr.jt_state_pending, running: tr.jt_state_running,
                             success: tr.jt_state_success, failed: tr.jt_state_failed };
        const chipClass = { success: "success", failed: "failed", running: "running" };

        const rows = allIps.map(ip => {
            const s = byIp[ip];
            const st = s ? s.status : "pending";
            const cls = chipClass[st] || "pending";
            const beat = st === "running" ? "beat" : "";
            const label = stateLabel[st] || st;
            return `<li class="jt-srow"><span class="jt-ip">${ip}</span>` +
                   `<span class="jt-chip ${cls}"><span class="d ${beat}"></span>${label}</span></li>`;
        }).join("");

        let ico;
        if (trayJob.status === "running") ico = `<span class="jt-ico running"></span>`;
        else if (trayJob.status === "completed") ico = `<span class="jt-ico completed">${JT_SVG.check}</span>`;
        else if (trayJob.status === "killed") ico = `<span class="jt-ico killed">${JT_SVG.stop}</span>`;
        else ico = `<span class="jt-ico failed">${JT_SVG.cross}</span>`;

        const meta = [`${doneC}/${totalC || "?"} ${tr.jt_servers}`, `⏱ ${jtElapsed()}`];
        if (okC) meta.push(`<span class="ok">✓ ${okC}</span>`);
        if (badC) meta.push(`<span class="bad">✗ ${badC}</span>`);

        const xTitle = trayJob.status === "running" ? tr.jt_stop : tr.jt_close;
        const openTitle = trayJob.status === "running" ? tr.jt_open_live : "";

        host.innerHTML =
            `<div class="jt-card ${trayJob.status}">` +
              `<div class="jt-head">` +
                `<button type="button" class="jt-open" title="${openTitle}">` +
                  ico +
                  `<span class="jt-titles">` +
                    `<span class="jt-name">${trayJob.name || "Automation"}</span>` +
                    `<span class="jt-meta">${meta.join(" · ")}</span>` +
                  `</span>` +
                `</button>` +
                `<button type="button" class="jt-x" title="${xTitle}">${JT_SVG.x}</button>` +
              `</div>` +
              `<div class="jt-bar"><i style="width:${pct}%"></i></div>` +
              (allIps.length ? `<ul class="jt-servers">${rows}</ul>` : "") +
            `</div>`;

        // Inline handlers can't see IIFE-scoped functions, so bind here.
        const openBtn = host.querySelector(".jt-open");
        const xBtn = host.querySelector(".jt-x");
        if (openBtn) openBtn.onclick = trayOpen;
        if (xBtn) xBtn.onclick = trayX;
    }

    function executeRun(payload) {
        const tr = t();
        runStarting = true;
        resetProgressUI();
        const nameEl = document.getElementById("wiz-live-script-name");
        if (nameEl) nameEl.textContent = selectedScript ? selectedScript.name : "";
        const terminal = document.getElementById("console-terminal");
        terminal.innerHTML = `<div class="terminal-line system-msg">> [SYSTEM] ${tr.toast_start}</div>`;
        showToast(tr.toast_start, "warning");
        wizShowStep("live");

        fetch("/api/run", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        })
            .then(res => res.json())
            .then(resData => {
                runStarting = false;
                if (resData.error) {
                    terminal.innerHTML += `<div class="terminal-line stderr">> [ERROR] ${resData.error}</div>`;
                    resetExecutionState();
                    showError(tr.toast_failed, resData.error);
                    wizShowStep(4);
                    wizRenderStep4();
                    return;
                }

                activeRunId = resData.run_id;
                killBtn.style.display = "block";
                document.getElementById("nav-wizard").classList.add("has-active-run");
                trayStart(activeRunId, selectedScript ? selectedScript.name : "", payloadServerCount(payload));
                connectLogStream(activeRunId);
                startStatusPolling(activeRunId);
                if (["logs", "dashboard"].includes(currentPage)) loadReportsAndHistory();
            })
            .catch(err => {
                runStarting = false;
                terminal.innerHTML += `<div class="terminal-line stderr">> [CONNECTION ERROR] ${err}</div>`;
                resetExecutionState();
                showError(tr.error_connection, err);
                wizShowStep(4);
                wizRenderStep4();
            });
    }

    killBtn.addEventListener("click", async () => {
        if (!activeRunId) return;
        const tr = t();
        const confirmed = await showConfirm(tr.confirm_kill_title, tr.confirm_kill_body, tr.btn_confirm_kill, true);
        if (!confirmed) return;

        fetch(`/api/kill/${activeRunId}`, { method: "POST" })
            .then(res => res.json())
            .then(data => {
                if (data.error) showError(data.error, "");
                else {
                    showToast(tr.toast_killed, "warning");
                    if (["logs", "dashboard"].includes(currentPage)) loadReportsAndHistory();
                }
            })
            .catch(err => showError(tr.error_connection, err));
    });

    // Single source of truth for "the run ended". Called by BOTH the SSE
    // 'finish' event AND the 1-second status poller - whichever notices first
    // wins, and the Set guard makes the heavy handling (state reset + summary
    // modal) run exactly once. This is what keeps the idle auto-logout working:
    // if we relied on the SSE 'finish' event alone and it was missed (tab
    // throttled, stream dropped/proxied), activeRunId stayed set forever and
    // automationActive() blocked the logout permanently.
    let finalizedRunIds = new Set();
    function finalizeRun(runId, finalStatus) {
        if (!runId || finalizedRunIds.has(runId)) return;
        finalizedRunIds.add(runId);
        if (eventSource) { try { eventSource.close(); } catch (e) {} eventSource = null; }
        stopStatusPolling();
        const status = finalStatus || { status: "failed" };
        lastRunSummary = status;
        try { updateProgressUI(status); } catch (e) {}
        trayFinish(runId, status);
        resetExecutionState(status.status || "failed");
        loadReportsAndHistory();
        loadTimeSaved();   // a finished run may have added to the cumulative time saved
        if (currentPage === "dashboard") loadDashboardAnalytics();   // refresh charts
        // Only pop the summary modal when we actually have a full status object
        // (the server-unreachable fallback passes only {status:"failed"}).
        if (status.script_name || status.servers) showRunSummary(status);
    }

    function startStatusPolling(runId) {
        stopStatusPolling();
        let consecutiveErrors = 0;
        statusPollTimer = setInterval(() => {
            fetch(`/api/status/${runId}`)
                .then(res => res.json())
                .then(status => {
                    consecutiveErrors = 0;
                    if (status.error) return;
                    updateProgressUI(status);
                    // Authoritative terminal-state clear: the poller (not the
                    // fragile SSE 'finish' event) guarantees activeRunId is
                    // released within ~1s of the run ending, so the idle
                    // auto-logout can never be blocked by a stuck run state.
                    if (status.status && status.status !== "running") {
                        finalizeRun(runId, status);
                    }
                })
                .catch(() => {
                    // Server unreachable (crashed / restarted): after ~15s give
                    // up so a stuck 'running' state can't block idle logout.
                    if (++consecutiveErrors >= 15) finalizeRun(runId, { status: "failed" });
                });
        }, 1000);
    }

    function stopStatusPolling() {
        if (statusPollTimer) {
            clearInterval(statusPollTimer);
            statusPollTimer = null;
        }
    }

    function connectLogStream(runId) {
        if (eventSource) eventSource.close();

        eventSource = new EventSource(`/api/stream/${runId}`);
        const terminal = document.getElementById("console-terminal");

        eventSource.onmessage = function (event) {
            appendTerminalLine(terminal, event.data);
        };

        eventSource.addEventListener("finish", function (event) {
            const statusStr = event.data;
            // Fetch the full final status for the summary, then hand off to the
            // single finalizeRun() path (deduped against the poller).
            fetch(`/api/status/${runId}`)
                .then(res => res.json())
                .then(finalStatus => finalizeRun(runId, finalStatus))
                .catch(() => finalizeRun(runId, { status: statusStr }));
        });

        eventSource.onerror = function () {
            // A transient stream error must NOT end the run - the browser can
            // hiccup mid-run. Close the dead stream, but let the 1s status
            // poller stay the single authority on when the run actually ends
            // (real terminal status, or server-unreachable timeout). That is
            // what reliably clears activeRunId and unblocks the idle logout.
            if (eventSource) { try { eventSource.close(); } catch (e) {} eventSource = null; }
        };
    }

    function appendTerminalLine(terminal, text) {
        const lineEl = document.createElement("div");

        if (text.startsWith("[SERVER-OK]") || text.startsWith("SUCCESS") || text.includes("Report successfully saved") || text.includes("has been finished")) {
            lineEl.className = "terminal-line success-msg";
        } else if (text.startsWith("[SERVER-FAIL]") || text.startsWith("ERROR") || text.includes("Exception:") || text.includes("Traceback") || text.includes("FAILED") || text.includes("Failed")) {
            lineEl.className = "terminal-line stderr";
        } else if (text.startsWith("[") || text.startsWith("INFO") || text.startsWith("Connecting") || text.startsWith("Starting")) {
            lineEl.className = "terminal-line system-msg";
        } else {
            lineEl.className = "terminal-line stdout";
        }

        // Tag each line with the server it belongs to (parallel runs prefix
        // every server's line with "[<ip>]"). Untagged lines (system/markers/
        // banners) are "sys" and always shown. This lets a chip click filter
        // the console down to one server's session.
        const ipMatch = text.match(/^\[(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\]/);
        lineEl.dataset.server = ipMatch ? ipMatch[1] : "sys";
        if (activeServerFilter && lineEl.dataset.server !== "sys" && lineEl.dataset.server !== activeServerFilter) {
            lineEl.style.display = "none";
        }

        lineEl.textContent = text;
        terminal.appendChild(lineEl);

        while (terminal.children.length > 3000) {
            terminal.removeChild(terminal.firstChild);
        }
        terminal.scrollTop = terminal.scrollHeight;
    }

    // Show only the selected server's lines (plus shared "sys" lines), or the
    // full merged log when ip is null (the "All" chip).
    function applyServerFilter(ip) {
        activeServerFilter = ip || null;
        document.querySelectorAll("#server-status-panel .server-chip").forEach(c => {
            c.classList.toggle("chip-active", (c.dataset.ip || "") === (activeServerFilter || ""));
        });
        const terminal = document.getElementById("console-terminal");
        if (!terminal) return;
        terminal.querySelectorAll(".terminal-line").forEach(line => {
            const s = line.dataset.server || "sys";
            line.style.display = (!activeServerFilter || s === "sys" || s === activeServerFilter) ? "" : "none";
        });
        terminal.scrollTop = terminal.scrollHeight;
    }

    function resetExecutionState(finalStatus = "ready") {
        killBtn.style.display = "none";
        document.getElementById("nav-wizard").classList.remove("has-active-run");
        WIZ.finished = true;

        const tr = t();
        if (finalStatus === "completed") {
            showToast(tr.toast_success, "success");
        } else if (finalStatus === "failed") {
            showToast(tr.toast_failed, "danger");
        } else if (finalStatus === "killed") {
            showToast(tr.toast_killed, "warning");
        }

        activeRunId = null;
        runStarting = false;
    }

    // =====================================================================
    // Run summary modal
    // =====================================================================
    function buildSummaryText(status) {
        const tr = t();
        const servers = status.servers || [];
        const ok = servers.filter(s => s.status === "success");
        const fail = servers.filter(s => s.status !== "success");
        const outputs = status.outputs || [];

        const lines = [];
        lines.push(`${tr.summary_title}: ${status.script_name}`);
        lines.push(`${tr.th_status}: ${status.status}`);
        lines.push(`${tr.summary_started}: ${status.started_at || "-"}`);
        lines.push(`${tr.summary_ended}: ${status.end_time || "-"}`);
        lines.push(`${tr.summary_duration}: ${status.duration || "-"}`);
        lines.push(`${tr.summary_user}: ${status.user || "-"} @ ${status.computer || "-"}`);
        lines.push("");
        lines.push(`${tr.summary_success_actions} (${ok.length}):`);
        ok.forEach(s => lines.push(`  ✔ ${s.ip} ${s.hostname ? "(" + s.hostname + ")" : ""}`));
        if (!ok.length) lines.push(`  ${tr.summary_none}`);
        lines.push(`${tr.summary_failed_actions} (${fail.length}):`);
        fail.forEach(s => lines.push(`  ✘ ${s.ip} ${s.reason ? "- " + s.reason : ""}`));
        if (!fail.length) lines.push(`  ${tr.summary_none}`);
        lines.push("");
        lines.push(`${tr.summary_reports_section}:`);
        outputs.forEach(o => lines.push(`  ${o.name} -> ${o.path}`));
        if (!outputs.length) lines.push(`  ${tr.summary_no_reports}`);
        return lines.join("\n");
    }

    function showRunSummary(status) {
        const tr = t();
        const overlay = document.getElementById("summary-modal");
        document.getElementById("summary-modal-title").textContent = `${tr.summary_title} - ${status.script_name || ""}`;

        const servers = status.servers || [];
        const ok = servers.filter(s => s.status === "success");
        const fail = servers.filter(s => s.status !== "success");
        const outputs = status.outputs || [];
        const targets = status.target_servers || [];

        let bannerClass = "ok", bannerText = tr.summary_success_banner;
        if (status.status === "failed") { bannerClass = "fail"; bannerText = tr.summary_failed_banner; }
        else if (status.status === "killed") { bannerClass = "fail"; bannerText = tr.summary_killed_banner; }

        const body = document.getElementById("summary-modal-body");
        body.innerHTML = `
            <div class="summary-status-banner ${bannerClass}">${bannerText}</div>
            ${status.fail_reason ? `<div class="modal-body-text" style="color:var(--accent-danger);font-weight:600;">${tr.th_fail_reason}: ${status.fail_reason}</div>` : ""}
            <div class="summary-section">
                <div class="summary-kv">
                    <span class="k">${tr.summary_what_ran}</span><span class="v">${status.script_name || "-"}</span>
                    <span class="k">${tr.summary_started}</span><span class="v">${status.started_at || "-"}</span>
                    <span class="k">${tr.summary_ended}</span><span class="v">${status.end_time || "-"}</span>
                    <span class="k">${tr.summary_duration}</span><span class="v">${status.duration || "-"}</span>
                    <span class="k">${tr.summary_user}</span><span class="v">${status.user || "-"}</span>
                    <span class="k">${tr.details_computer}</span><span class="v">${status.computer || "-"}</span>
                </div>
            </div>
            <div class="summary-section">
                <h4>${tr.summary_servers_section} ${targets.length ? "(" + targets.length + ")" : ""}</h4>
                ${servers.length ? `
                    <ul class="summary-list">
                        ${ok.map(s => `<li class="ok"><span>✔ ${s.ip} ${s.hostname ? "(" + s.hostname + ")" : ""}</span><span>${s.log || ""}</span></li>`).join("")}
                        ${fail.map(s => `<li class="fail"><span>✘ ${s.ip} ${s.reason ? "- " + s.reason : ""}</span><span>${s.log || ""}</span></li>`).join("")}
                    </ul>
                ` : (targets.length ? `<div class="modal-body-text">${targets.join(", ")}</div>` : `<div class="modal-body-text">${tr.summary_no_servers}</div>`)}
            </div>
            <div class="summary-section">
                <h4>${tr.summary_reports_section}</h4>
                ${outputs.length ? `
                    <ul class="summary-list">
                        ${outputs.map(o => `<li class="ok"><span>${o.name}<br><small style="color:var(--text-muted)">${o.path}</small></span><button class="btn btn-secondary btn-xs summary-open-file" data-path="${o.path}">${tr.btn_open}</button></li>`).join("")}
                    </ul>
                ` : `<div class="modal-body-text">${tr.summary_no_reports}</div>`}
            </div>
        `;

        body.querySelectorAll(".summary-open-file").forEach(btn => {
            btn.addEventListener("click", () => openReportLocally(btn.getAttribute("data-path")));
        });

        document.getElementById("summary-copy-btn").textContent = tr.btn_copy_summary;
        document.getElementById("summary-open-reports-btn").textContent = tr.btn_open_all_reports;
        document.getElementById("summary-goto-logs-btn").textContent = tr.btn_goto_logs;
        overlay.style.display = "flex";
    }

    document.getElementById("summary-modal-close").addEventListener("click", () => {
        document.getElementById("summary-modal").style.display = "none";
    });

    document.getElementById("summary-goto-logs-btn").addEventListener("click", () => {
        document.getElementById("summary-modal").style.display = "none";
        switchPage("logs");
    });

    document.getElementById("summary-copy-btn").addEventListener("click", () => {
        if (!lastRunSummary) return;
        const text = buildSummaryText(lastRunSummary);
        const done = () => showToast(t().toast_copied, "success");
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
        } else {
            fallbackCopy(text, done);
        }
    });

    function fallbackCopy(text, done) {
        const ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); done(); } catch (e) { showError("Copy", e); }
        ta.remove();
    }

    document.getElementById("summary-open-reports-btn").addEventListener("click", () => {
        const outputs = (lastRunSummary && lastRunSummary.outputs) || [];
        if (!outputs.length) {
            openReportLocally("RESULTS_DIR");   // no outputs -> just open the Outputs folder
            return;
        }
        // Open the Outputs folder and highlight exactly the output this run
        // produced. All of a run's files share one run sub-folder, so revealing
        // the first output highlights that run folder (i.e. the whole result).
        revealRunOutput(outputs[0].path);
    });

    // =====================================================================
    // Auto-close: signal the server when this tab/window is actually closed
    // (not just refreshed) so the background process doesn't linger. Uses
    // pagehide (fires reliably on close/refresh/navigate) + sendBeacon for
    // delivery even during unload. The server waits a short grace period
    // and cancels the shutdown if any new request arrives (i.e. a refresh).
    // =====================================================================
    window.addEventListener("pagehide", () => {
        try {
            if (navigator.sendBeacon) {
                navigator.sendBeacon("/api/shutdown-signal");
            }
        } catch (e) { /* best-effort only */ }
    });

    // Heartbeat: a tiny ping every few seconds proves this tab is still open.
    // The server exits once heartbeats stop for a while - a robust backstop
    // for the beacon above, which Chrome skips on pages that were never
    // interacted with. (In a background tab Chrome throttles this to ~1/min;
    // the server-side threshold accounts for that.)
    setInterval(() => { fetch("/api/ping").catch(() => {}); }, 5000);

    // =====================================================================
    // Initialize application
    // =====================================================================
    loadCommandProfiles().then(() => {
        loadDefaultCredentials();
        loadAllScripts();
        Promise.all([loadEnvironment(), loadCompanies()]).then(() => {
            translateUI();
            switchPage(currentPage);   // honor the URL hash (e.g. after refresh)
            startHistoryPolling();
        });
    });
});

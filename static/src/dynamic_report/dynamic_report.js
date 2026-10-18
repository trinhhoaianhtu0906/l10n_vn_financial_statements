import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const PERIODS = [
    { key: "year", label: "Cả năm", months: [1, 12] },
    { key: "q1", label: "Quý 1", months: [1, 3] },
    { key: "q2", label: "Quý 2", months: [4, 6] },
    { key: "q3", label: "Quý 3", months: [7, 9] },
    { key: "q4", label: "Quý 4", months: [10, 12] },
    { key: "h1", label: "6 tháng đầu năm", months: [1, 6] },
    { key: "m9", label: "9 tháng đầu năm", months: [1, 9] },
    ...Array.from({ length: 12 }, (_, i) => ({ key: `m${i + 1}x`, label: `Tháng ${i + 1}`, months: [i + 1, i + 1] })),
    { key: "custom", label: "Tùy chọn ngày" },
];

function pad(n) {
    return String(n).padStart(2, "0");
}

function lastDay(year, month) {
    return new Date(year, month, 0).getDate();
}

/**
 * On-screen TT99 statements: the figures load when the menu opens and are
 * recomputed whenever a filter changes; Excel and PDF use the same figures.
 */
export class VnTt99DynamicReport extends Component {
    static template = "l10n_vn_tt99_reports.DynamicReport";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.actionService = useService("action");
        this.notification = useService("notification");
        this.periods = PERIODS;
        this.state = useState({
            choices: null,
            options: {},
            year: new Date().getFullYear(),
            period: "year",
            data: null,
            loading: false,
            error: null,
            exporting: false,
        });
        this.requestId = 0;
        onWillStart(async () => {
            const choices = await this.orm.call("vn.report.wizard", "get_dynamic_options", []);
            const context = this.props.action?.context || {};
            const reports = choices.reports;
            const wanted = reports.find((r) => r.id === context.default_report_id);
            const report = wanted || reports.find((r) => !r.consolidated) || reports[0];
            this.state.choices = choices;
            this.state.year = Number(choices.date_to.slice(0, 4));
            this.state.options = {
                report_id: report?.id,
                company_id: choices.company_id,
                consolidation_group_id: context.default_consolidation_group_id || choices.groups[0]?.id || false,
                date_from: choices.date_from,
                date_to: choices.date_to,
                target_move: "posted",
                hide_zero: false,
                include_branches: true,
                comparison: "default",
                comparison_count: 1,
                show_variance: false,
            };
            await this.load();
        });
    }

    // -- getters ----------------------------------------------------------

    get report() {
        return this.state.choices?.reports.find((r) => r.id === this.state.options.report_id);
    }

    get standaloneReports() {
        return this.state.choices.reports.filter((r) => !r.consolidated);
    }

    get consolidatedReports() {
        return this.state.choices.reports.filter((r) => r.consolidated);
    }

    get years() {
        const current = new Date().getFullYear();
        return Array.from({ length: 8 }, (_, i) => current + 1 - i);
    }

    // -- loading ----------------------------------------------------------

    serverOptions() {
        const options = { ...this.state.options };
        if (!this.report?.consolidated) {
            options.consolidation_group_id = false;
        }
        if (!this.state.choices.groups.length) {
            delete options.consolidation_group_id;
        }
        return options;
    }

    async load() {
        if (!this.report) {
            return;
        }
        if (this.report.consolidated && !this.state.options.consolidation_group_id) {
            this.state.data = null;
            this.state.error = "Chưa có nhóm hợp nhất. Hãy tạo nhóm trong menu Hợp nhất > Nhóm hợp nhất.";
            return;
        }
        const requestId = ++this.requestId;
        this.state.loading = true;
        try {
            const data = await this.orm.call("vn.report.wizard", "get_dynamic_report", [this.serverOptions()]);
            if (requestId === this.requestId) {
                this.state.data = data;
                this.state.error = null;
            }
        } catch (error) {
            if (requestId === this.requestId) {
                this.state.data = null;
                this.state.error = error.data?.message || error.message || String(error);
            }
        } finally {
            if (requestId === this.requestId) {
                this.state.loading = false;
            }
        }
    }

    // -- filters ----------------------------------------------------------

    applyPeriod() {
        const period = PERIODS.find((p) => p.key === this.state.period);
        if (!period?.months) {
            return;
        }
        const year = this.state.year;
        const [first, last] = period.months;
        this.state.options.date_from = `${year}-${pad(first)}-01`;
        this.state.options.date_to = `${year}-${pad(last)}-${pad(lastDay(year, last))}`;
    }

    async onReportChange(ev) {
        this.state.options.report_id = Number(ev.target.value);
        this.applyPeriod();
        await this.load();
    }

    async onOptionChange(key, ev) {
        const target = ev.target;
        let value = target.type === "checkbox" ? target.checked : target.value;
        if (["company_id", "consolidation_group_id", "comparison_count"].includes(key)) {
            value = Number(value);
        }
        this.state.options[key] = value;
        await this.load();
    }

    async onYearChange(ev) {
        this.state.year = Number(ev.target.value);
        if (this.state.period === "custom") {
            this.state.period = "year";
        }
        this.applyPeriod();
        await this.load();
    }

    async onPeriodChange(ev) {
        this.state.period = ev.target.value;
        this.applyPeriod();
        if (this.state.period !== "custom") {
            await this.load();
        }
    }

    async onDateChange(key, ev) {
        if (!ev.target.value) {
            return;
        }
        this.state.options[key] = ev.target.value;
        if (this.state.options.date_from <= this.state.options.date_to) {
            await this.load();
        }
    }

    // -- actions ----------------------------------------------------------

    async export(method) {
        if (!this.state.data || this.state.exporting) {
            return;
        }
        this.state.exporting = true;
        try {
            const action = await this.orm.call("vn.report.wizard", method, [[this.state.data.wizard_id]]);
            await this.actionService.doAction(action);
        } finally {
            this.state.exporting = false;
        }
    }

    async openLine(line) {
        if (!line.drillable || line.blank) {
            return;
        }
        const result = await this.orm.call("vn.report.wizard.line", "get_drilldown", [[line.id]]);
        if (result.inactive_companies.length) {
            this.notification.add(
                `Danh sách chỉ hiện bút toán của các công ty đang bật. Bật thêm ở bộ chọn công ty (góc phải trên) để xem: ${result.inactive_companies.join(", ")}.`,
                { type: "warning", sticky: false }
            );
        }
        await this.actionService.doAction(result.action);
    }

    format(value, blank) {
        if (blank) {
            return "";
        }
        const rounded = Math.round(value || 0);
        if (!rounded) {
            return "-";
        }
        const text = Math.abs(rounded).toLocaleString("vi-VN");
        return rounded < 0 ? `(${text})` : text;
    }

    formatCell(value, column, blank) {
        if (blank) {
            return "";
        }
        if (column.kind === "percent") {
            if (value === null || value === undefined) {
                return "";
            }
            return `${value.toLocaleString("vi-VN", { minimumFractionDigits: 1, maximumFractionDigits: 1 })}%`;
        }
        return this.format(value, false);
    }

    cellClass(value, column) {
        // No red/green: a higher figure is good for revenue but bad for expenses.
        return column.kind === "percent" ? "text-muted" : "";
    }

    indent(line) {
        return `padding-left: ${0.5 + Math.max(line.level - 1, 0) * 1.25}rem;`;
    }
}

registry.category("actions").add("l10n_vn_tt99_reports.dynamic_report", VnTt99DynamicReport);

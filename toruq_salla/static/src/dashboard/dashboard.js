/** @odoo-module **/
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, useState } from "@odoo/owl";

const LABELS = {
    en: {
        title: "Salla Dashboard",
        subtitle: "Odoo is the master. Salla follows.",
        refresh: "Refresh",
        pull: "Pull from Salla",
        sync: "Sync products",
        push: "Push stock",
        settings: "Store settings",
        test: "Test connection",
        no_store: "No Salla store yet. Create one in Configuration > Stores.",
        create_store: "Create store",
        connected: "Connected",
        draft: "Not connected",
        error: "Connection error",
        busy: "Working...",
        products: "Products in Salla",
        linked: "Linked to Odoo",
        unmatched: "Not in Odoo",
        missing: "Missing in Salla",
        on_sale: "On sale",
        hidden: "Hidden",
        out: "Out (status)",
        stock_total: "Current Salla stock",
        out_of_stock: "Out of stock",
        low_stock: "Low stock (5 or less)",
        mismatch: "Stock differences",
        published: "Published from Odoo",
        orders_total: "Salla orders",
        orders_week: "Orders (7 days)",
        sales_total: "Salla sales total",
        events_pending: "Pending events",
        events_failed: "Failed events",
        orders_chart: "Orders in the last 7 days",
        low_title: "Products running low in Salla",
        sku: "SKU",
        name: "Name",
        salla_qty: "Salla",
        odoo_qty: "Odoo",
        none: "Nothing to show",
        failed_title: "Failed events",
        info_title: "Store status",
        store: "Store",
        merchant: "Merchant",
        warehouse: "Warehouse",
        last_pull: "Last pull",
        last_push: "Last stock push",
        token: "Token expires",
        order_mode: "Order import",
        reserve: "Showroom reserve",
        stock_sync: "Stock sync",
        on: "On",
        off: "Off",
        mode_none: "Disabled",
        mode_quotation: "Quotations",
        mode_confirmed: "Confirmed orders",
        open_products: "Products",
        open_orders: "Orders",
        open_events: "Events",
        last_error: "Last error",
        done: "Done",
    },
    ar: {
        title: "لوحة تحكم سلة",
        subtitle: "أودو هو الأساس وسلة تتبعه.",
        refresh: "تحديث",
        pull: "سحب من سلة",
        sync: "مزامنة المنتجات",
        push: "إرسال المخزون",
        settings: "إعدادات المتجر",
        test: "اختبار الاتصال",
        no_store: "لا يوجد متجر سلة بعد. أنشئ واحدًا من الإعدادات > المتاجر.",
        create_store: "إنشاء متجر",
        connected: "متصل",
        draft: "غير متصل",
        error: "خطأ في الاتصال",
        busy: "جاري التنفيذ...",
        products: "المنتجات في سلة",
        linked: "مربوطة بأودو",
        unmatched: "غير موجودة في أودو",
        missing: "محذوفة من سلة",
        on_sale: "معروضة للبيع",
        hidden: "مخفية",
        out: "نافدة (الحالة)",
        stock_total: "مخزون سلة الحالي",
        out_of_stock: "نفد المخزون",
        low_stock: "مخزون منخفض (5 أو أقل)",
        mismatch: "فروقات المخزون",
        published: "المنشورة من أودو",
        orders_total: "طلبات سلة",
        orders_week: "الطلبات (7 أيام)",
        sales_total: "إجمالي مبيعات سلة",
        events_pending: "أحداث معلقة",
        events_failed: "أحداث فاشلة",
        orders_chart: "الطلبات خلال آخر 7 أيام",
        low_title: "منتجات قاربت على النفاد في سلة",
        sku: "الكود",
        name: "الاسم",
        salla_qty: "سلة",
        odoo_qty: "أودو",
        none: "لا يوجد ما يعرض",
        failed_title: "الأحداث الفاشلة",
        info_title: "حالة المتجر",
        store: "المتجر",
        merchant: "رقم التاجر",
        warehouse: "المخزن",
        last_pull: "آخر سحب",
        last_push: "آخر إرسال للمخزون",
        token: "انتهاء التوكن",
        order_mode: "استيراد الطلبات",
        reserve: "احتياطي المعرض",
        stock_sync: "مزامنة المخزون",
        on: "مفعّل",
        off: "معطّل",
        mode_none: "معطّل",
        mode_quotation: "عروض أسعار",
        mode_confirmed: "أوامر بيع مؤكدة",
        open_products: "المنتجات",
        open_orders: "الطلبات",
        open_events: "الأحداث",
        last_error: "آخر خطأ",
        done: "تم",
    },
};

class ToruqSallaDashboard extends Component {
    static template = "toruq_salla.Dashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        const lang = (document.documentElement.getAttribute("lang") || "en").toLowerCase();
        this.labels = LABELS[lang.startsWith("ar") ? "ar" : "en"];
        this.state = useState({ loading: true, busy: "", data: null, storeId: null });
        onWillStart(async () => {
            await this.load();
        });
    }

    tr(key) {
        return this.labels[key] || key;
    }

    fmt(value) {
        return new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(value || 0);
    }

    async load() {
        this.state.loading = true;
        try {
            this.state.data = await this.orm.call("toruq.salla.store", "get_dashboard_data", [this.state.storeId]);
            if (this.state.data.store) {
                this.state.storeId = this.state.data.store.id;
            }
        } finally {
            this.state.loading = false;
        }
    }

    get store() {
        return this.state.data && this.state.data.store;
    }

    get kpi() {
        return (this.state.data && this.state.data.kpi) || {};
    }

    get cards() {
        const k = this.kpi;
        const cur = this.store ? this.store.currency : "";
        const cards = [
            ["products", k.products, ""],
            ["linked", k.linked, "ok"],
            ["unmatched", k.unmatched, k.unmatched ? "warn" : ""],
            ["missing", k.missing, k.missing ? "bad" : ""],
            ["stock_total", k.stock_total, "hero"],
            ["out_of_stock", k.out_of_stock, k.out_of_stock ? "bad" : ""],
            ["low_stock", k.low_stock, k.low_stock ? "warn" : ""],
            ["mismatch", k.mismatch, k.mismatch ? "warn" : "ok"],
            ["on_sale", k.on_sale, ""],
            ["hidden", k.hidden, ""],
            ["published", k.published, ""],
            ["orders_total", k.orders_total, ""],
            ["orders_week", k.orders_week, ""],
            ["sales_total", this.fmt(k.sales_total) + " " + cur, "hero"],
            ["events_pending", k.events_pending, ""],
            ["events_failed", k.events_failed, k.events_failed ? "bad" : "ok"],
        ];
        return cards.map(([key, value, tone]) => ({
            key,
            tone,
            label: this.tr(key),
            value: typeof value === "string" ? value : this.fmt(value),
        }));
    }

    get maxOrders() {
        const series = (this.state.data && this.state.data.series) || [];
        return Math.max(1, ...series.map((s) => s.count));
    }

    barHeight(count) {
        return Math.max(4, Math.round((count / this.maxOrders) * 100));
    }

    get stateLabel() {
        return this.store ? this.tr(this.store.state) : "";
    }

    get modeLabel() {
        return this.store ? this.tr("mode_" + this.store.order_mode) : "";
    }

    async onStoreChange(ev) {
        this.state.storeId = parseInt(ev.target.value, 10);
        await this.load();
    }

    async runAction(method) {
        if (this.state.busy || !this.state.storeId) {
            return;
        }
        this.state.busy = method;
        try {
            const result = await this.orm.call("toruq.salla.store", method, [[this.state.storeId]]);
            if (result && result.type) {
                await this.action.doAction(result);
            }
        } finally {
            this.state.busy = "";
            await this.load();
        }
    }

    openStore() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "toruq.salla.store",
            res_id: this.state.storeId || false,
            views: [[false, "form"]],
            target: "current",
        });
    }

    openAction(xmlid) {
        this.action.doAction(xmlid);
    }
}

registry.category("actions").add("toruq_salla_dashboard", ToruqSallaDashboard);

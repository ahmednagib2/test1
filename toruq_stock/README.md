# toruq_stock

Odoo 18 add-on on top of `odoo_salla_inventory_app` (Tecfy Salla Odoo Inventory Connector). Author: ahmednagib.

Qty sent to Salla = max(0, Odoo available qty in Tecfy Salla locations - showroom reserve).
Odoo real stock, POS and accounting are never changed.

## Install (staging first)
1. Copy the `toruq_stock` folder next to `odoo_salla_inventory_app`, push, update apps list, install.
2. Company > Salla tab: enable safety stock, set default reserve (1 or 2).
3. Optional per product: Product > Salla Reserve tab.
4. Test on ONE product with a test store. Never leave Update Salla Quantity on with the production Merchant Id on staging.

## What it does
- Overrides prepareQuantities() (Tecfy 20 min cron) with the reserve rule.
- Overrides tryPost(): failed posts are retried (max 5), not marked as posted.
- New cron every minute: only products with recent stock moves are recalculated and posted.

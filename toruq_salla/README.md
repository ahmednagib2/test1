# toruq_salla

Odoo 18 <-> Salla integration for Turoq Al-Ghida. Author: Ahmed Nagib (أحمد نجيب). Odoo is the master.

## Features
- Store settings (one or many stores), OAuth tokens with safe automatic refresh, signed webhook receiver.
- Publish selected products only (product list > Action > Publish on Salla). Products are matched by SKU = Odoo Internal Reference.
- Keep names (in a chosen language), prices and descriptions in sync, per store option.
- Stock pushed from the warehouse / locations chosen on the store, with an optional showroom reserve.
- Orders from Salla: none / quotation / confirmed, plus optional invoice, payment and delivery automation.
- Orange dashboard, Arabic and English.

## Setup
1. Salla Partners > create a Private app (Easy Mode). Copy Client ID, Client Secret and Webhook Secret into the store form.
2. Set the app webhook URL to the one shown on the store form (https://YOUR-ODOO/toruq_salla/webhook).
3. Install the app on the store. The token arrives automatically. Press Test Connection.
4. Choose the warehouse, tick Publish on Salla on a sample of products, then press Pull Products and Sync Products.

## Notes
- Verify the Salla payload fields on a demo store before going live (order payload, stock bulk endpoint).
- Test on a staging database with a demo store first.

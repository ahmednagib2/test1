{
    'name': 'toruq_stock',
    'summary': 'Showroom safety stock for Salla quantity sent by Tecfy connector',
    'description': 'Sends to Salla: max(0, Odoo available qty - showroom reserve). Odoo real stock is never changed.',
    'category': 'Extra Tools',
    'version': '18.0.1.0.0',
    'author': 'ahmednagib',
    'license': 'LGPL-3',
    'depends': ['stock', 'odoo_salla_inventory_app'],
    'data': [
        'data/cron.xml',
        'views/res_company_views.xml',
        'views/product_views.xml',
    ],
    'installable': True,
    'application': False,
}

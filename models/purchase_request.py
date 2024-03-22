from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, get_lang, float_round
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT
from dateutil.relativedelta import relativedelta
import datetime


class PurchaseRequestOrder(models.Model):
    _name = "purchase.request.order"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _description = "Purchase Request Order"

    @api.depends('order_line.price_total')
    def _amount_all(self):
        for order in self:
            order_lines = order.order_line.filtered(lambda x: not x.display_type)

            if order.company_id.tax_calculation_rounding_method == 'round_globally':
                tax_results = self.env['account.tax']._compute_taxes([
                    line._convert_to_tax_base_line_dict()
                    for line in order_lines
                ])
                totals = tax_results['totals']
                amount_untaxed = totals.get(order.currency_id, {}).get('amount_untaxed', 0.0)
                amount_tax = totals.get(order.currency_id, {}).get('amount_tax', 0.0)
            else:
                amount_untaxed = sum(order_lines.mapped('price_subtotal'))
                amount_tax = sum(order_lines.mapped('price_tax'))

            order.amount_untaxed = amount_untaxed
            order.amount_tax = amount_tax
            order.amount_total = order.amount_untaxed + order.amount_tax

    partner_id = fields.Many2one('res.partner', string='Customer', required=True, change_default=True, tracking=True,
                                 domain="['|', ('company_id', '=', False), ('company_id', '=', company_id),('partner_type','=','customer')]",
                                 help="You can find a vendor by its Name, TIN, Email or Internal Reference.")

    name = fields.Char(string="Reference No.", required=True, copy=False, readonly=True, default=lambda self: _('New'))
    note = fields.Text(string="description")
    date_order = fields.Datetime('Order Deadline', required=True, index=True, copy=False,
                                 default=fields.Datetime.now,
                                 help="Depicts the date within which the Quotation should be confirmed and converted into a purchase order.")
    currency_id = fields.Many2one('res.currency', 'Currency', required=True,
                                  default=lambda self: self.env.company.currency_id.id)
    order_line = fields.One2many('purchase.request.order.line', 'order_id', string='Order Lines',
                                 copy=True)
    # vendor_line = fields.One2many('vendor.line', 'vendor_id', string='Vendor Lines',
    #                               copy=True)
    date_planned = fields.Datetime(
        string='Expected Arrival', index=True, copy=False, compute='_compute_date_planned', store=True, readonly=False,
        tracking=True,
        help="Delivery date promised by vendor. This date is used to determine expected arrival of products.")
    origin = fields.Char('Source Document', copy=False,
                         help="Reference of the document that generated this purchase order "
                              "request (e.g. a sales order)")
    user_id = fields.Many2one(
        'res.users', string='Buyer', index=True, tracking=True,
        default=lambda self: self.env.user, check_company=True)
    company_id = fields.Many2one('res.company', 'Company', required=True, index=True,
                                 default=lambda self: self.env.company.id)
    notes = fields.Html('Terms and Conditions')
    incoterm_id = fields.Many2one('account.incoterms', 'Incoterm', states={'done': [('readonly', True)]},
                                  help="International Commercial Terms are a series of predefined commercial terms used in international transactions.")
    fiscal_position_id = fields.Many2one('account.fiscal.position', string='Fiscal Position',
                                         domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]")
    payment_term_id = fields.Many2one('account.payment.term', 'Payment Terms',
                                      domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]")
    amount_untaxed = fields.Monetary(string='Untaxed Amount', store=True, readonly=True, compute='_amount_all',
                                     tracking=True)
    tax_totals = fields.Binary(compute='_compute_tax_totals', exportable=False)
    amount_tax = fields.Monetary(string='Taxes', store=True, readonly=True, compute='_amount_all')
    amount_total = fields.Monetary(string='Total', store=True, readonly=True, compute='_amount_all')
    tax_country_id = fields.Many2one(
        comodel_name='res.country',
        compute='_compute_tax_country_id',
        # Avoid access error on fiscal position, when reading a purchase order with company != user.company_ids
        compute_sudo=True,
        help="Technical field to filter the available taxes depending on the fiscal country and fiscal position.")
    state = fields.Selection([('draft', 'Draft'), ('rfq', 'RFQ'), ('confirm', 'Confirm'),
                              ], string="State", default='draft', tracking=True)
    request_id = fields.Many2one('purchase.order', 'Purchase Request')

    def action_confirm(self):
        self.state = 'rfq'

    def unlink(self):
        for record in self:
            if record.state != 'draft':
                raise UserError('You can only delete Records in Draft State.')
        return super(PurchaseRequestOrder, self).unlink()

    def create_so(self):
        pl = self.env['product.pricelist'].search([('name', '=', 'Default AED pricelist')], limit=1)
        so_vals = {
            'partner_id': self.partner_id.id,
            'date_order': self.date_order,
            'currency_id': self.currency_id.id,
            'user_id': self.user_id.id,
            'company_id': self.company_id.id,
            
            
            'fiscal_position_id': self.fiscal_position_id.id,
            'request_id': self.id,
            'order_line': [],
        }

        for line in self.order_line:
            order_line_vals = {
                'purchase_request_line_id': line.id,
                'product_id': line.product_id.id,
                'name': line.name,
                'product_uom_qty': line.quantity,
                'product_uom': line.product_uom.id,
                # 'price_unit': line.price_unit,
                'tax_id': [(6, 0, line.taxes_id.ids)],
            }
            so_vals['order_line'].append((0, 0, order_line_vals))
        so = self.env['sale.order'].create(so_vals)
        return {
            'name': _('Sale Order'),
            'view_mode': 'form',
            'view_type': 'form',
            'res_model': 'sale.order',
            'res_id': so.id,
            'type': 'ir.actions.act_window',
        }

    def create_rfq(self):
        print("oooooooooooooooooooooooooooooooooooooooooooooo")
        action = {
            'type': 'ir.actions.act_window',
            'name': 'Select Vendors',
            'res_model': 'wizard_vendor',
            'view_mode': 'form',
            'target': 'new',
            # 'res_id': self.id

        }
        return action

    # def create_rfq(self):
    #     rfq_vals = {
    #         'date_order': self.date_order,
    #         'currency_id': self.currency_id.id,
    #         'date_planned': self.date_planned,
    #         'user_id': self.user_id.id,
    #         'company_id': self.company_id.id,
    #         'payment_term_id': self.payment_term_id.id,
    #         'fiscal_position_id': self.fiscal_position_id.id,
    #         'request_id': self.id,
    #         'partner_id': self.partner_id.id,
    #     }
    #     rfq = self.env['purchase.rfq'].create(rfq_vals)
    #     for line in self.order_line:
    #         order_line_vals = {
    #             # 'purchase_request_line_id': line.id,
    #             'product_id': line.product_id.id,
    #             'name': line.name,
    #             'quantity': line.quantity,
    #             'product_uom': line.product_uom.id,
    #             # 'product_packaging_qty': line.product_packaging_qty,
    #             # 'product_packaging_id': line.product_packaging_id.id,
    #             'price_unit': line.price_unit,
    #             'taxes_id': [(6, 0, line.taxes_id.ids)],
    #             'order_id': rfq.id
    #         }
    #         self.env['purchase.rfq.line'].create(order_line_vals)
    #         # rfq_vals['order_line'].append((0, 0, order_line_vals))
    #
    #     action = {
    #         'type': 'ir.actions.act_window',
    #         'name': 'Create RFQ',
    #         'res_model': 'purchase.rfq',
    #         'view_mode': 'form',
    #         'target': 'current',
    #         'res_id': rfq.id
    #
    #     }
    #     return action

    @api.model
    def create(self, vals):
        if not vals.get('note'):
            vals['note'] = 'New Form'
        if vals.get('name', _('New')) == _('New'):
            vals['name'] = self.env['ir.sequence'].next_by_code('purchase.request.order') or _('New')
            res = super(PurchaseRequestOrder, self).create(vals)
            return res

    @api.depends('order_line.taxes_id', 'order_line.price_subtotal', 'amount_total', 'amount_untaxed')
    def _compute_tax_totals(self):
        for order in self:
            order_lines = order.order_line.filtered(lambda x: not x.display_type)
            order.tax_totals = self.env['account.tax']._prepare_tax_totals(
                [x._convert_to_tax_base_line_dict() for x in order_lines],
                order.currency_id or order.company_id.currency_id,
            )

    @api.depends('company_id.account_fiscal_country_id', 'fiscal_position_id.country_id',
                 'fiscal_position_id.foreign_vat')
    def _compute_tax_country_id(self):
        for record in self:
            if record.fiscal_position_id.foreign_vat:
                record.tax_country_id = record.fiscal_position_id.country_id
            else:
                record.tax_country_id = record.company_id.account_fiscal_country_id

    @api.depends('order_line.date_planned')
    def _compute_date_planned(self):
        """ date_planned = the earliest date_planned across all order lines. """
        for order in self:
            dates_list = order.order_line.filtered(lambda x: not x.display_type and x.date_planned).mapped(
                'date_planned')
            if dates_list:
                order.date_planned = min(dates_list)
            else:
                order.date_planned = False

    def open_purchase_orders(self):
        lst = []

        rfqs = self.env['purchase.rfq'].search([('request_id', '=', self.id)]).ids
        for rf in rfqs:
            lst.extend(self.env['purchase.order'].search([('request_id', '=', rf)]).ids)
        return {
            'type': 'ir.actions.act_window',
            'name': 'Purchase Order',
            'res_model': 'purchase.order',
            'view_mode': 'tree,form',
            'target': 'current',
            'domain': [('id', 'in', lst)],

        }

    def open_rfq(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Purchase Request For Quotation',
            'res_model': 'purchase.rfq',
            'view_mode': 'tree,form',
            'target': 'current',
            'domain': [('request_id', '=', self.id)],
            'context': {
                'default_request_id': self.id, }
        }

    def open_so(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Sale Order',
            'res_model': 'sale.order',
            'view_mode': 'tree,form',
            'target': 'current',
            'domain': [('request_id', '=', self.id)]
        }


class PurchaseRequestOrderLine(models.Model):
    _name = 'purchase.request.order.line'
    _inherit = 'analytic.mixin'
    _description = 'Purchase Request Order Line'

    product_id = fields.Many2one('product.product', string='Product', domain=[('purchase_ok', '=', True)],
                                 change_default=True, index='btree_not_null')
    name = fields.Text('Description', compute='_compute_description', store=True, readonly=False)
    quantity = fields.Float('Quantity', digits='Product Unit of Measure', required=True, store=True,
                            default="1")
    date_planned = fields.Datetime(
        string='Expected Arrival', index=True,
        compute="_compute_price_unit_and_date_planned_and_name", readonly=False, store=True, tracking=True,
        help="Delivery date expected from vendor. This date respectively defaults to vendor pricelist lead time then today's date.")
    taxes_id = fields.Many2many('account.tax', string='Taxes',
                                domain=['|', ('active', '=', False), ('active', '=', True)])
    product_uom_category_id = fields.Many2one(related='product_id.uom_id.category_id')
    product_uom = fields.Many2one('uom.uom', string='Unit of Measure', required=True,
                                  domain="[('category_id', '=', product_uom_category_id)]")
    order_id = fields.Many2one('purchase.request.order', string='Order Reference', index=True, required=True,
                               ondelete='cascade')

    company_id = fields.Many2one('res.company', string='Company', store=True,
                                 readonly=True)
    price_unit = fields.Float(
        string='Unit Price', digits='Product Price',
     readonly=False, store=True)

    purchase_price = fields.Float(string='Purchase Price', digits='Product Price', compute="_compute_purchase_price",)
    sale_price = fields.Float(string='Sale Price', digits='Product Price', compute="_compute_sale_price",)
    price_subtotal = fields.Monetary(compute='_compute_amount', string='Subtotal', store=True)
    price_total = fields.Monetary(compute='_compute_amount', string='Total', store=True)
    product_packaging_qty = fields.Float('Packaging Quantity', compute="_compute_product_packaging_qty", store=True,
                                         readonly=False)
    product_packaging_id = fields.Many2one('product.packaging', string='Packaging',
                                           domain="[('product_id', '=', product_id)]",
                                           check_company=True,
                                           compute="_compute_product_packaging_id", store=True, readonly=False)
    date_order = fields.Datetime(related='order_id.date_order', string='Order Date', readonly=True)

    sequence = fields.Integer(string='Sequence', default=10)
    currency_id = fields.Many2one(related='order_id.currency_id', store=True, string='Currency', readonly=True)
    product_template_id = fields.Many2one(
        string="Product Template",
        related='product_id.product_tmpl_id',
        domain=[('purchase_ok', '=', True)])
    price_tax = fields.Float(compute='_compute_amount', string='Tax', store=True)
    display_type = fields.Selection([
        ('line_section', "Section"),
        ('line_note', "Note")], default=False, help="Technical field for UX purpose.")


    def _compute_purchase_price(self):
        for rec in self:
            print()
            rec.purchase_price =0

    def _compute_sale_price(self):
        for rec in self:
            print()
            rec.sale_price = 0

    @api.depends('product_id')
    def _compute_description(self):
        for each in self:
            if each.product_id:
                each.name = each.product_id.name
                each.product_uom = each.product_id.uom_po_id.id
            else:
                each.name = ''

    def _convert_to_tax_base_line_dict(self):
        self.ensure_one()
        return self.env['account.tax']._convert_to_tax_base_line_dict(
            self,
            currency=self.order_id.currency_id,
            product=self.product_id,
            taxes=self.taxes_id,
            price_unit=self.price_unit,
            quantity=self.quantity,
            price_subtotal=self.price_subtotal,
        )

    @api.depends('product_id', 'product_uom')
    def _compute_product_packaging_id(self):
        for line in self:
            # remove packaging if not match the product
            if line.product_packaging_id.product_id != line.product_id:
                line.product_packaging_id = False
            # suggest biggest suitable packaging
            if line.product_id and line.quantity and line.product_uom:
                line.product_packaging_id = line.product_id.packaging_ids.filtered(
                    'purchase')._find_suitable_product_packaging(line.quantity,
                                                                 line.product_uom) or line.product_packaging_id

    @api.depends('product_packaging_id', 'product_uom', 'quantity')
    def _compute_product_packaging_qty(self):
        for line in self:
            if not line.product_packaging_id:
                line.product_packaging_qty = 0
            else:
                packaging_uom = line.product_packaging_id.product_uom_id
                packaging_uom_qty = line.product_uom._compute_quantity(line.quantity, packaging_uom)
                line.product_packaging_qty = float_round(packaging_uom_qty / line.product_packaging_id.qty,
                                                         precision_rounding=packaging_uom.rounding)

    @api.depends('quantity', 'price_unit', 'taxes_id')
    def _compute_amount(self):
        for line in self:
            tax_results = self.env['account.tax']._compute_taxes([line._convert_to_tax_base_line_dict()])
            totals = list(tax_results['totals'].values())[0]
            amount_untaxed = totals['amount_untaxed']
            amount_tax = totals['amount_tax']

            line.update({
                'price_subtotal': amount_untaxed,
                'price_tax': amount_tax,
                'price_total': amount_untaxed + amount_tax,
            })

    @api.model
    def _get_date_planned(self, seller, po=False):
        date_order = po.date_order if po else self.order_id.date_order
        if date_order:
            return date_order + relativedelta(days=seller.delay if seller else 0)
        else:
            return datetime.today() + relativedelta(days=seller.delay if seller else 0)

    @api.depends('quantity', 'product_uom')
    def _compute_price_unit_and_date_planned_and_name(self):
        for line in self:
            if not line.product_id:
                continue
            params = {'order_id': line.order_id}
            seller = line.product_id._select_seller(
                quantity=line.quantity,
                date=line.order_id.date_order and line.order_id.date_order.date(),
                uom_id=line.product_uom,
                params=params)

            if seller or not line.date_planned:
                line.date_planned = line._get_date_planned(seller).strftime(DEFAULT_SERVER_DATETIME_FORMAT)

            # If not seller, use the standard price. It needs a proper currency conversion.
            if not seller:
                po_line_uom = line.product_uom or line.product_id.uom_po_id
                price_unit = line.env['account.tax']._fix_tax_included_price_company(
                    line.product_id.uom_id._compute_price(line.product_id.standard_price, po_line_uom),
                    line.product_id.supplier_taxes_id,
                    line.taxes_id,
                    line.company_id,
                )
                price_unit = line.product_id.currency_id._convert(
                    price_unit,
                    line.currency_id,
                    line.company_id,
                    line.date_order,
                    False
                )
                line.price_unit = float_round(price_unit, precision_digits=max(line.currency_id.decimal_places,
                                                                               self.env[
                                                                                   'decimal.precision'].precision_get(
                                                                                   'Product Price')))
                continue

            price_unit = line.env['account.tax']._fix_tax_included_price_company(seller.price,
                                                                                 line.product_id.supplier_taxes_id,
                                                                                 line.taxes_id,
                                                                                 line.company_id) if seller else 0.0
            price_unit = seller.currency_id._convert(price_unit, line.currency_id, line.company_id, line.date_order,
                                                     False)
            price_unit = float_round(price_unit, precision_digits=max(line.currency_id.decimal_places,
                                                                      self.env['decimal.precision'].precision_get(
                                                                          'Product Price')))
            line.price_unit = seller.product_uom._compute_price(price_unit, line.product_uom)


# class VendorLine(models.Model):
#     _name = 'vendor.line'
#     _description = 'Vendor Line'
#
#     name = fields.Many2one('res.partner', 'Vendor')
#     vendor_id = fields.Many2one('purchase.request.order', string='Vendor Reference', index=True, required=True)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    request_id = fields.Many2one('purchase.request.order', 'Purchase Request')

    @api.model_create_multi
    def create(self, vals):
        res = super(SaleOrder, self).create(vals)
        for line in res.order_line:
            if not line.product_id.taxes_id:
                request_line = self.env['purchase.request.order.line'].search([
                    ('order_id', '=', res.request_id.id),
                    ('product_id', '=', line.product_id.id)
                ], limit=1)
                if request_line:
                    line.tax_id = [(6, 0, request_line.taxes_id.ids)]
        return res


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    request_id = fields.Many2one('purchase.rfq', 'Purchase Request')

    def action_open_rma(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'RMA',
            'res_model': 'rma.main',
            'domain': [('helpdesk_id', '=', self.id)],
            'view_mode': 'tree,form',
            'target': 'current',
            'context': {'search_default_helpdesk_id': self.id, 'default_helpdesk_id': self.id, },
        }

    def open_rfq(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Purchase Request For Quotation',
            'res_model': 'purchase.rfq',
            'view_mode': 'tree,form',
            'target': 'current',
            'domain': [('id', '=', self.request_id.id)],
            'context': {
                'default_id': self.request_id.id, }
        }

    request_order_id = fields.Many2one('purchase.request.order', 'Related Purchase Request Order', readonly=True)

    def open_request(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Purchase Request Orders',
            'res_model': 'purchase.request.order',
            'view_mode': 'tree,form',
            'target': 'current',
            'domain': [('id', '=', self.request_order_id.id)],
        }


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    purchase_request_line_id = fields.Many2one('purchase.rfq.line', store=True)


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    purchase_request_line_id = fields.Many2one('purchase.request.order.line', store=True)


class TierDefinition(models.Model):
    _inherit = "tier.definition"

    @api.model
    def _get_tier_validation_model_names(self):
        res = super(TierDefinition, self)._get_tier_validation_model_names()
        res.append("purchase.request.order")
        return res


class PurchaseRequest(models.Model):
    _name = "purchase.request.order"
    _inherit = ["purchase.request.order", "tier.validation"]
    _state_from = ["draft"]
    _state_to = ["confirm", "approved"]

    request_id = fields.Many2one('purchase.rfq', 'Purchase Request')
    # purchase_id = fields.Many2one('purchase.order', 'Purchase Request')

    _tier_validation_manual_config = False


class PurchaseRFQ(models.Model):
    _name = "purchase.rfq"
    _inherit = ['portal.mixin', 'product.catalog.mixin', 'mail.thread', 'mail.activity.mixin']
    _description = "Purchase RFQ"
    _rec_names_search = ['name', 'partner_ref']

    @api.depends('order_line.price_total')
    def _amount_all(self):
        for order in self:
            order_lines = order.order_line.filtered(lambda x: not x.display_type)

            if order.company_id.tax_calculation_rounding_method == 'round_globally':
                tax_results = self.env['account.tax']._compute_taxes([
                    line._convert_to_tax_base_line_dict()
                    for line in order_lines
                ])
                totals = tax_results['totals']
                amount_untaxed = totals.get(order.currency_id, {}).get('amount_untaxed', 0.0)
                amount_tax = totals.get(order.currency_id, {}).get('amount_tax', 0.0)
            else:
                amount_untaxed = sum(order_lines.mapped('price_subtotal'))
                amount_tax = sum(order_lines.mapped('price_tax'))

            order.amount_untaxed = amount_untaxed
            order.amount_tax = amount_tax
            order.amount_total = order.amount_untaxed + order.amount_tax

    request_id = fields.Many2one('purchase.request.order', 'Purchase Request')
    partner_id = fields.Many2one('res.partner', string='Vendor', required=True, change_default=True, tracking=True,
                                 domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
                                 help="You can find a vendor by its Name, TIN, Email or Internal Reference.")

    name = fields.Char(string="Reference No.", required=True, copy=False, readonly=True, default=lambda self: _('New'))
    note = fields.Text(string="description")
    date_order = fields.Datetime('Order Deadline', required=True, index=True, copy=False,
                                 default=fields.Datetime.now,
                                 help="Depicts the date within which the Quotation should be confirmed and converted into a purchase order.")
    currency_id = fields.Many2one('res.currency', 'Currency', required=True,
                                  default=lambda self: self.env.company.currency_id.id)
    order_line = fields.One2many('purchase.rfq.line', 'order_id', string='Order Lines',
                                 copy=True)
    # vendor_line = fields.One2many('vendor.line', 'vendor_id', string='Vendor Lines',
    #                               copy=True)
    date_planned = fields.Datetime(
        string='Expected Arrival', index=True, copy=False, compute='_compute_date_planned', store=True, readonly=False,
        tracking=True,
        help="Delivery date promised by vendor. This date is used to determine expected arrival of products.")
    origin = fields.Char('Source Document', copy=False,
                         help="Reference of the document that generated this purchase order "
                              "request (e.g. a sales order)")
    user_id = fields.Many2one(
        'res.users', string='Buyer', index=True, tracking=True,
        default=lambda self: self.env.user, check_company=True)
    company_id = fields.Many2one('res.company', 'Company', required=True, index=True,
                                 default=lambda self: self.env.company.id)
    notes = fields.Html('Terms and Conditions')
    incoterm_id = fields.Many2one('account.incoterms', 'Incoterm', states={'done': [('readonly', True)]},
                                  help="International Commercial Terms are a series of predefined commercial terms used in international transactions.")
    fiscal_position_id = fields.Many2one('account.fiscal.position', string='Fiscal Position',
                                         domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]")
    payment_term_id = fields.Many2one('account.payment.term', 'Payment Terms',
                                      domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]")
    amount_untaxed = fields.Monetary(string='Untaxed Amount', store=True, readonly=True, compute='_amount_all',
                                     tracking=True)
    tax_totals = fields.Binary(compute='_compute_tax_totals', exportable=False)
    amount_tax = fields.Monetary(string='Taxes', store=True, readonly=True, compute='_amount_all')
    amount_total = fields.Monetary(string='Total', store=True, readonly=True, compute='_amount_all')
    tax_country_id = fields.Many2one(
        comodel_name='res.country',
        compute='_compute_tax_country_id',
        # Avoid access error on fiscal position, when reading a purchase order with company != user.company_ids
        compute_sudo=True,
        help="Technical field to filter the available taxes depending on the fiscal country and fiscal position.")
    state = fields.Selection([('draft', 'Draft'), ('confirm', 'Confirm'), ('done', 'Done'),
                              ], string="State", default='draft', tracking=True)
    purchase_order_id = fields.Many2one('purchase.order')

    def action_confirm(self):
        self.state = 'confirm'

    def unlink(self):
        for record in self:
            if record.state != 'draft':
                raise UserError('You can only delete Records in Draft State.')
        return super(PurchaseRequestOrder, self).unlink()

    def create_rfq(self):
        rfq_vals = {
            'date_order': self.date_order,
            'currency_id': self.currency_id.id,
            'date_planned': self.date_planned,
            'user_id': self.user_id.id,
            'company_id': self.company_id.id,
            'payment_term_id': self.payment_term_id.id,
            'fiscal_position_id': self.fiscal_position_id.id,
            'request_id': self.id,
            'order_line': [],
            'partner_id': self.partner_id.id,
            'request_order_id': self.request_id.id,
        }

        for line in self.order_line:
            order_line_vals = {
                'purchase_request_line_id': line.id,
                'product_id': line.product_id.id,
                'name': line.name,
                'product_qty': line.quantity,
                'product_uom': line.product_uom.id,
                'product_packaging_qty': line.product_packaging_qty,
                'product_packaging_id': line.product_packaging_id.id,
                'price_unit': line.price_unit,
                'taxes_id': [(6, 0, line.taxes_id.ids)],
            }
            rfq_vals['order_line'].append((0, 0, order_line_vals))
        po = self.env['purchase.order'].create(rfq_vals)
        action = {
            'type': 'ir.actions.act_window',
            'name': 'Create RFQ',
            'res_model': 'purchase.order',
            'view_mode': 'form',
            'target': 'current',
            'res_id': po.id
            # 'context': {
            #     'default_request_id': rfq_vals.get('request_id'),
            #     'default_date_order': rfq_vals.get('date_order'),
            #     'default_currency_id': rfq_vals.get('currency_id'),
            #     'default_date_planned': rfq_vals.get('date_planned'),
            #     'default_user_id': rfq_vals.get('user_id'),
            #     'default_company_id': rfq_vals.get('company_id'),
            #     'default_payment_term_id': rfq_vals.get('payment_term_id'),
            #     'default_fiscal_position_id': rfq_vals.get('fiscal_position_id'),
            #     'default_order_line': rfq_vals.get('order_line'),
            # }
        }
        return action

    @api.model
    def create(self, vals):
        if not vals.get('note'):
            vals['note'] = 'New Form'
        if vals.get('name', _('New')) == _('New'):
            vals['name'] = self.env['ir.sequence'].next_by_code('purchase.rfq') or _('New')
            res = super(PurchaseRFQ, self).create(vals)
            return res

    @api.depends('order_line.taxes_id', 'order_line.price_subtotal', 'amount_total', 'amount_untaxed')
    def _compute_tax_totals(self):
        for order in self:
            order_lines = order.order_line.filtered(lambda x: not x.display_type)
            order.tax_totals = self.env['account.tax']._prepare_tax_totals(
                [x._convert_to_tax_base_line_dict() for x in order_lines],
                order.currency_id or order.company_id.currency_id,
            )

    @api.depends('company_id.account_fiscal_country_id', 'fiscal_position_id.country_id',
                 'fiscal_position_id.foreign_vat')
    def _compute_tax_country_id(self):
        for record in self:
            if record.fiscal_position_id.foreign_vat:
                record.tax_country_id = record.fiscal_position_id.country_id
            else:
                record.tax_country_id = record.company_id.account_fiscal_country_id

    @api.depends('order_line.date_planned')
    def _compute_date_planned(self):
         # date_planned = the earliest date_planned across all order lines.
        for order in self:
            dates_list = order.order_line.filtered(lambda x: not x.display_type and x.date_planned).mapped(
                'date_planned')
            if dates_list:
                order.date_planned = min(dates_list)
            else:
                order.date_planned = False

    def open_purchase_orders(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Purchase Order',
            'res_model': 'purchase.order',
            'view_mode': 'tree,form',
            'target': 'current',
            'domain': [('request_id', '=', self.id)],
            'context': {
                'default_request_id': self.id, }
        }

    # def open_rfq(self):
    #     return {
    #         'type': 'ir.actions.act_window',
    #         'name': 'Request For Quotation',
    #         'res_model': 'purchase.order',
    #         'view_mode': 'tree,form',
    #         'target': 'current',
    #         'domain': [('request_id', '=', self.id), ('state', '=', 'draft')]
    #     }
    def open_request(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Purchase Request ',
            'res_model': 'purchase.request.order',
            'view_mode': 'tree,form',
            'target': 'current',
            'domain': [('id', '=', self.request_id.id)],
            'context': {
                'default_id': self.request_id.id, }
        }


class PurchaseRFQLine(models.Model):
    _name = 'purchase.rfq.line'
    _inherit = 'analytic.mixin'
    _description = 'Purchase Order Line'
    _order = 'order_id, sequence, id'

    purchase_request_line_id = fields.Many2one('purchase.request.order.line', store=True)
    product_id = fields.Many2one('product.product', string='Product', domain=[('purchase_ok', '=', True)],
                                 change_default=True, index='btree_not_null')
    name = fields.Text('Description', compute='_compute_description', store=True, readonly=False)
    quantity = fields.Float('Quantity', digits='Product Unit of Measure', required=True, store=True,
                            default="1")
    date_planned = fields.Datetime(
        string='Expected Arrival', index=True,
        compute="_compute_price_unit_and_date_planned_and_name", readonly=False, store=True, tracking=True,
        help="Delivery date expected from vendor. This date respectively defaults to vendor pricelist lead time then today's date.")
    taxes_id = fields.Many2many('account.tax', string='Taxes',
                                domain=['|', ('active', '=', False), ('active', '=', True)])
    product_uom_category_id = fields.Many2one(related='product_id.uom_id.category_id')
    product_uom = fields.Many2one('uom.uom', string='Unit of Measure', required=True,
                                  domain="[('category_id', '=', product_uom_category_id)]")
    order_id = fields.Many2one('purchase.rfq', string='Order Reference', index=True, required=True,
                               ondelete='cascade')

    company_id = fields.Many2one('res.company', string='Company', store=True,
                                 readonly=True)
    price_unit = fields.Float(
        string='Unit Price', required=True, digits='Product Price',
        compute="_compute_price_unit_and_date_planned_and_name", readonly=False, store=True)
    price_subtotal = fields.Monetary(compute='_compute_amount', string='Subtotal', store=True)
    price_total = fields.Monetary(compute='_compute_amount', string='Total', store=True)
    product_packaging_qty = fields.Float('Packaging Quantity', compute="_compute_product_packaging_qty", store=True,
                                         readonly=False)
    product_packaging_id = fields.Many2one('product.packaging', string='Packaging',
                                           domain="[('product_id', '=', product_id)]",
                                           check_company=True,
                                           compute="_compute_product_packaging_id", store=True, readonly=False)
    date_order = fields.Datetime(related='order_id.date_order', string='Order Date', readonly=True)

    sequence = fields.Integer(string='Sequence', default=10)
    currency_id = fields.Many2one(related='order_id.currency_id', store=True, string='Currency', readonly=True)
    product_template_id = fields.Many2one(
        string="Product Template",
        related='product_id.product_tmpl_id',
        domain=[('purchase_ok', '=', True)])
    price_tax = fields.Float(compute='_compute_amount', string='Tax', store=True)
    display_type = fields.Selection([
        ('line_section', "Section"),
        ('line_note', "Note")], default=False, help="Technical field for UX purpose.")

    @api.depends('product_id')
    def _compute_description(self):
        for each in self:
            if each.product_id:
                each.name = each.product_id.name
                each.product_uom = each.product_id.uom_po_id.id
            else:
                each.name = ''

    def _convert_to_tax_base_line_dict(self):
        self.ensure_one()
        return self.env['account.tax']._convert_to_tax_base_line_dict(
            self,
            currency=self.order_id.currency_id,
            product=self.product_id,
            taxes=self.taxes_id,
            price_unit=self.price_unit,
            quantity=self.quantity,
            price_subtotal=self.price_subtotal,
        )

    @api.depends('product_id', 'product_uom')
    def _compute_product_packaging_id(self):
        for line in self:
            # remove packaging if not match the product
            if line.product_packaging_id.product_id != line.product_id:
                line.product_packaging_id = False
            # suggest biggest suitable packaging
            if line.product_id and line.quantity and line.product_uom:
                line.product_packaging_id = line.product_id.packaging_ids.filtered(
                    'purchase')._find_suitable_product_packaging(line.quantity,
                                                                 line.product_uom) or line.product_packaging_id

    @api.depends('product_packaging_id', 'product_uom', 'quantity')
    def _compute_product_packaging_qty(self):
        for line in self:
            if not line.product_packaging_id:
                line.product_packaging_qty = 0
            else:
                packaging_uom = line.product_packaging_id.product_uom_id
                packaging_uom_qty = line.product_uom._compute_quantity(line.quantity, packaging_uom)
                line.product_packaging_qty = float_round(packaging_uom_qty / line.product_packaging_id.qty,
                                                         precision_rounding=packaging_uom.rounding)

    @api.depends('quantity', 'price_unit', 'taxes_id')
    def _compute_amount(self):
        for line in self:
            tax_results = self.env['account.tax']._compute_taxes([line._convert_to_tax_base_line_dict()])
            totals = list(tax_results['totals'].values())[0]
            amount_untaxed = totals['amount_untaxed']
            amount_tax = totals['amount_tax']

            line.update({
                'price_subtotal': amount_untaxed,
                'price_tax': amount_tax,
                'price_total': amount_untaxed + amount_tax,
            })

    @api.model
    def _get_date_planned(self, seller, po=False):
        date_order = po.date_order if po else self.order_id.date_order
        if date_order:
            return date_order + relativedelta(days=seller.delay if seller else 0)
        else:
            return datetime.today() + relativedelta(days=seller.delay if seller else 0)

    @api.depends('quantity', 'product_uom')
    def _compute_price_unit_and_date_planned_and_name(self):
        for line in self:
            if not line.product_id:
                continue
            params = {'order_id': line.order_id}
            seller = line.product_id._select_seller(
                quantity=line.quantity,
                date=line.order_id.date_order and line.order_id.date_order.date(),
                uom_id=line.product_uom,
                params=params)

            if seller or not line.date_planned:
                line.date_planned = line._get_date_planned(seller).strftime(DEFAULT_SERVER_DATETIME_FORMAT)

            # If not seller, use the standard price. It needs a proper currency conversion.
            if not seller:
                po_line_uom = line.product_uom or line.product_id.uom_po_id
                price_unit = line.env['account.tax']._fix_tax_included_price_company(
                    line.product_id.uom_id._compute_price(line.product_id.standard_price, po_line_uom),
                    line.product_id.supplier_taxes_id,
                    line.taxes_id,
                    line.company_id,
                )
                price_unit = line.product_id.currency_id._convert(
                    price_unit,
                    line.currency_id,
                    line.company_id,
                    line.date_order,
                    False
                )
                line.price_unit = float_round(price_unit, precision_digits=max(line.currency_id.decimal_places,
                                                                               self.env[
                                                                                   'decimal.precision'].precision_get(
                                                                                   'Product Price')))
                continue

            price_unit = line.env['account.tax']._fix_tax_included_price_company(seller.price,
                                                                                 line.product_id.supplier_taxes_id,
                                                                                 line.taxes_id,
                                                                                 line.company_id) if seller else 0.0
            price_unit = seller.currency_id._convert(price_unit, line.currency_id, line.company_id, line.date_order,
                                                     False)
            price_unit = float_round(price_unit, precision_digits=max(line.currency_id.decimal_places,
                                                                      self.env['decimal.precision'].precision_get(
                                                                          'Product Price')))
            line.price_unit = seller.product_uom._compute_price(price_unit, line.product_uom)

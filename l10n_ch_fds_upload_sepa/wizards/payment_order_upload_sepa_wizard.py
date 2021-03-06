# -*- coding: utf-8 -*-
# © 2015-2017 Compassion CH (Nicolas Tran, Emanuel Cino)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).
from tempfile import mkstemp

import openerp

from odoo import models, fields, api, exceptions, _
import logging
import base64
import os


_logger = logging.getLogger(__name__)

try:
    import pysftp
    SFTP_OK = True
except ImportError:
    SFTP_OK = False
    _logger.debug(
        'This module needs pysftp to connect to the FDS. '
        'Please install pysftp on your system. (sudo pip install pysftp)'
    )


class PaymenOrderUploadSepaWizard(models.TransientModel):
    """ Allows to upload the generated SEPA electronic payment
        file for Switzerland to FDS Postfinance.
    """
    _name = 'payment.order.upload.sepa.wizard'
    _description = 'Upload SEPA to FDS'

    fds_account_id = fields.Many2one(
        'fds.postfinance.account', 'FDS Account',
        default=lambda self: self._get_default_account()
    )
    fds_directory_id = fields.Many2one(
        'fds.postfinance.directory', 'FDS Directory',
        help='Select one upload directory. Be sure to have at least one '
             'directory configured with upload access rights.'
    )
    attachment_id = fields.Many2one(
        'ir.attachment', required=True, ondelete='cascade'
    )
    file_data = fields.Binary(
        'Generated File', related='attachment_id.datas', readonly=True
    )
    filename = fields.Char(
        related='attachment_id.name', readonly=True
    )
    payment_order_id = fields.Many2one(
        'account.payment.order', 'Payment Order', required=True, readonly=True,
        ondelete='cascade'
    )
    state = fields.Selection([
        ('upload', 'upload'),
        ('finish', 'finish')], readonly=True, default='upload'
    )

    ##################################
    #         Button action          #
    ##################################
    @api.multi
    def upload_generate_file_button(self):
        ''' upload pain_001 file to the FDS Postfinance by sftp

            :returns action: configuration for the next wizard's view
            :raises Warning:
                - If no fds account and directory selected
                - if current user do not have key
                - if unable to connect to sftp
        '''
        self.ensure_one()
        if not SFTP_OK:
            raise exceptions.Warning(
                _("Please install pysftp to use this feature."))
        (key, key_pass) = self._get_sftp_key()

        # create tmp file
        key_fd, key_file = mkstemp()
        data_fd, data_file = mkstemp()
        try:
            os.write(key_fd, base64.b64decode(key.private_key_crypted))
            os.write(data_fd, base64.b64decode(self.attachment_id.datas))
        finally:
            os.close(key_fd)
            os.close(data_fd)

        # upload to sftp
        with pysftp.Connection(
                self.fds_account_id.hostname,
                username=self.fds_account_id.username,
                private_key=key_file,
                private_key_pass=key_pass) as sftp:

            with sftp.cd(self.fds_directory_id.name):
                # Here we want to commit to make sure we see
                # the file was uploaded and avoid sending it again.
                with api.Environment.manage():
                    with openerp.registry(
                            self.env.cr.dbname).cursor() as new_cr:
                        new_env = api.Environment(
                            new_cr, self.env.uid, self.env.context)
                        wizard = self.with_env(new_env)
                        sftp.put(data_file,
                                 remotepath=self.attachment_id.name.replace(
                                     '/', '-'))
                        wizard.state = 'finish'
                        _logger.info("[OK] upload file (%s) ", wizard.filename)

        self._add2historical()

        os.remove(key_file)
        os.remove(data_file)

        self.payment_order_id.generated2uploaded()
        return True

    ##############################
    #          function          #
    ##############################
    def _get_default_account(self):
        """ Select one account if only one exists. """
        fds_accounts = self.env['fds.postfinance.account'].search([])
        if len(fds_accounts) == 1:
            return fds_accounts.id
        else:
            return False

    @api.onchange('fds_account_id')
    def _get_default_upload_directory(self):
        self.ensure_one()
        if self.fds_account_id:
            default_directory = self.fds_account_id.directory_ids.filtered(
                lambda d: d.name == 'pain-001-in' and
                d.allow_upload_file)
            if len(default_directory) == 1:
                self.fds_directory_id = default_directory

    @api.multi
    def _get_sftp_key(self):
        ''' private function that get the SFTP key needed for connection
            with the server.

            :returns (str key, str key_pass):
            :riases Warning: if no key found
        '''
        fds_authentication_key_obj = self.env['fds.authentication.keys']
        key = fds_authentication_key_obj.search([
            ['user_id', '=', self.env.user.id],
            ['fds_account_id', '=', self.fds_account_id.id]])

        if not key:
            raise exceptions.Warning(_("You don't have an authentication key"))

        if not key.key_active:
            raise exceptions.Warning(_('Authentication key not active'))

        key_pass = fds_authentication_key_obj.config()
        return (key, key_pass)

    @api.multi
    def _add2historical(self):
        ''' private function that add the upload file to historic

            :returns: None
        '''
        self.ensure_one()
        values = {
            'payment_order_id': self.payment_order_id.id,
            'fds_account_id': self.fds_account_id.id,
            'filename': self.filename,
            'directory_id': self.fds_directory_id.id,
            'state': 'uploaded'}
        historical_dd_obj = self.env['fds.sepa.upload.history']
        historical_dd_obj.create(values)

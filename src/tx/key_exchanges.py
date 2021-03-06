#!/usr/bin/env python3.6
# -*- coding: utf-8 -*-

"""
Copyright (C) 2013-2017  Markus Ottela

This file is part of TFC.

TFC is free software: you can redistribute it and/or modify it under the terms
of the GNU General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version.

TFC is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR
PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with TFC. If not, see <http://www.gnu.org/licenses/>.
"""

import os
import time
import typing

from typing import Dict

import nacl.bindings
import nacl.encoding
import nacl.public

from src.common.crypto       import argon2_kdf, csprng, encrypt_and_sign, hash_chain
from src.common.db_masterkey import MasterKey
from src.common.exceptions   import FunctionReturn
from src.common.input        import ask_confirmation_code, get_b58_key, nh_bypass_msg, yes
from src.common.output       import box_print, c_print, clear_screen, message_printer, print_key
from src.common.output       import phase, print_fingerprint, print_on_previous_line
from src.common.path         import ask_path_gui
from src.common.statics      import *

from src.tx.packet import queue_command, queue_to_nh

if typing.TYPE_CHECKING:
    from multiprocessing        import Queue
    from src.common.db_contacts import ContactList
    from src.common.db_settings import Settings
    from src.tx.windows         import TxWindow


def new_local_key(contact_list: 'ContactList',
                  settings:     'Settings',
                  queues:       Dict[bytes, 'Queue']) -> None:
    """Run Tx-side local key exchange protocol.

    Local key encrypts commands and data sent from TxM to RxM. The key is
    delivered to RxM in packet encrypted with an ephemeral symmetric key.

    The checksummed Base58 format key decryption key is typed on RxM manually.
    This prevents local key leak in following scenarios:

        1. CT is intercepted by adversary on compromised NH but no visual
           eavesdropping takes place.
        2. CT is not intercepted by adversary on NH but visual eavesdropping
           records decryption key.
        3. CT is delivered from TxM to RxM (compromised NH is bypassed) and
           visual eavesdropping records decryption key.

    Once correct key decryption key is entered on RxM, Receiver program will
    display the 1-byte confirmation code generated by Transmitter program.
    The code will be entered on TxM to confirm user has successfully delivered
    the key decryption key.

    The protocol is completed with Transmitter program sending an ACK message
    to Receiver program, that then moves to wait for public keys from contact.
    """
    try:
        if settings.session_traffic_masking and contact_list.has_local_contact:
            raise FunctionReturn("Error: Command is disabled during traffic masking.")

        clear_screen()
        c_print("Local key setup", head=1, tail=1)

        c_code = os.urandom(1)
        key    = csprng()
        hek    = csprng()
        kek    = csprng()
        packet = LOCAL_KEY_PACKET_HEADER + encrypt_and_sign(key + hek + c_code, key=kek)

        nh_bypass_msg(NH_BYPASS_START, settings)
        queue_to_nh(packet, settings, queues[NH_PACKET_QUEUE])

        while True:
            print_key("Local key decryption key (to RxM)", kek, settings)
            purp_code = ask_confirmation_code()
            if purp_code == c_code.hex():
                break
            elif purp_code == RESEND:
                phase("Resending local key", head=2)
                queue_to_nh(packet, settings, queues[NH_PACKET_QUEUE])
                phase(DONE)
                print_on_previous_line(reps=(9 if settings.local_testing_mode else 10))
            else:
                box_print(["Incorrect confirmation code. If RxM did not receive",
                           "encrypted local key, resend it by typing 'resend'."], head=1)
                print_on_previous_line(reps=(11 if settings.local_testing_mode else 12), delay=2)

        nh_bypass_msg(NH_BYPASS_STOP, settings)

        # Add local contact to contact list database
        contact_list.add_contact(LOCAL_ID, LOCAL_ID, LOCAL_ID,
                                 bytes(FINGERPRINT_LEN), bytes(FINGERPRINT_LEN),
                                 False, False, False)

        # Add local contact to keyset database
        queues[KEY_MANAGEMENT_QUEUE].put((KDB_ADD_ENTRY_HEADER, LOCAL_ID,
                                          key, csprng(),
                                          hek, csprng()))

        # Notify RxM that confirmation code was successfully entered
        queue_command(LOCAL_KEY_INSTALLED_HEADER, settings, queues[COMMAND_PACKET_QUEUE])

        box_print("Successfully added a new local key.")
        clear_screen(delay=1)

    except KeyboardInterrupt:
        raise FunctionReturn("Local key setup aborted.", delay=1, head=3, tail_clear=True)


def verify_fingerprints(tx_fp: bytes, rx_fp: bytes) -> bool:
    """\
    Verify fingerprints over out-of-band channel to
    detect MITM attacks against TFC's key exchange.

    :param tx_fp: User's fingerprint
    :param rx_fp: Contact's fingerprint
    :return:      True if fingerprints match, else False
    """
    clear_screen()

    message_printer("To verify received public key was not replaced by attacker in network, "
                    "call the contact over end-to-end encrypted line, preferably Signal "
                    "(https://signal.org/). Make sure Signal's safety numbers have been "
                    "verified, and then verbally compare the key fingerprints below.", head=1, tail=1)

    print_fingerprint(tx_fp, "         Your fingerprint (you read)         ")
    print_fingerprint(rx_fp, "Purported fingerprint for contact (they read)")

    return yes("Is the contact's fingerprint correct?")


def start_key_exchange(account:      str,
                       user:         str,
                       nick:         str,
                       contact_list: 'ContactList',
                       settings:     'Settings',
                       queues:       Dict[bytes, 'Queue']) -> None:
    """Start X25519 key exchange with recipient.

    Variable naming:

        tx     = user's key                 rx  = contact's key
        sk     = private (secret) key       pk  = public key
        key    = message key                hek = header key
        dh_ssk = X25519 shared secret

    :param account:      The contact's account name (e.g. alice@jabber.org)
    :param user:         The user's account name (e.g. bob@jabber.org)
    :param nick:         Contact's nickname
    :param contact_list: Contact list object
    :param settings:     Settings object
    :param queues:       Dictionary of multiprocessing queues
    :return:             None
    """
    try:
        tx_sk = nacl.public.PrivateKey(csprng())
        tx_pk = bytes(tx_sk.public_key)

        while True:
            queue_to_nh(PUBLIC_KEY_PACKET_HEADER
                        + tx_pk
                        + user.encode()
                        + US_BYTE
                        + account.encode(),
                        settings, queues[NH_PACKET_QUEUE])

            rx_pk = get_b58_key(B58_PUB_KEY, settings)

            if rx_pk != RESEND.encode():
                break

        if rx_pk == bytes(KEY_LENGTH):
            # Public key is zero with negligible probability, therefore we
            # assume such key is malicious and attempts to either result in
            # zero shared key (pointless considering implementation), or to
            # DoS the key exchange as libsodium does not accept zero keys.
            box_print(["Warning!",
                       "Received a malicious public key from network.",
                       "Aborting key exchange for your safety."], tail=1)
            raise FunctionReturn("Error: Zero public key", output=False)

        dh_box = nacl.public.Box(tx_sk, nacl.public.PublicKey(rx_pk))
        dh_ssk = dh_box.shared_key()

        # Domain separate each key with key-type specific context variable
        # and with public keys that both clients know which way to place.
        tx_key = hash_chain(dh_ssk + rx_pk + b'message_key')
        rx_key = hash_chain(dh_ssk + tx_pk + b'message_key')

        tx_hek = hash_chain(dh_ssk + rx_pk + b'header_key')
        rx_hek = hash_chain(dh_ssk + tx_pk + b'header_key')

        # Domain separate fingerprints of public keys by using the shared
        # secret as salt. This way entities who might monitor fingerprint
        # verification channel are unable to correlate spoken values with
        # public keys that transit through a compromised IM server. This
        # protects against de-anonymization of IM accounts in cases where
        # clients connect to the compromised server via Tor. The preimage
        # resistance of hash chain protects the shared secret from leaking.
        tx_fp = hash_chain(dh_ssk + tx_pk + b'fingerprint')
        rx_fp = hash_chain(dh_ssk + rx_pk + b'fingerprint')

        if not verify_fingerprints(tx_fp, rx_fp):
            box_print(["Warning!",
                       "Possible man-in-the-middle attack detected.",
                       "Aborting key exchange for your safety."], tail=1)
            raise FunctionReturn("Error: Fingerprint mismatch", output=False)

        packet = KEY_EX_X25519_HEADER \
                 + tx_key + tx_hek \
                 + rx_key + rx_hek \
                 + account.encode() + US_BYTE + nick.encode()

        queue_command(packet, settings, queues[COMMAND_PACKET_QUEUE])

        contact_list.add_contact(account, user, nick,
                                 tx_fp, rx_fp,
                                 settings.log_messages_by_default,
                                 settings.accept_files_by_default,
                                 settings.show_notifications_by_default)

        # Use random values as Rx-keys to prevent decryption if they're accidentally used.
        queues[KEY_MANAGEMENT_QUEUE].put((KDB_ADD_ENTRY_HEADER, account,
                                          tx_key, csprng(),
                                          tx_hek, csprng()))

        box_print(f"Successfully added {nick}.")
        clear_screen(delay=1)

    except KeyboardInterrupt:
        raise FunctionReturn("Key exchange aborted.", delay=1, head=2, tail_clear=True)


def create_pre_shared_key(account:      str,
                          user:         str,
                          nick:         str,
                          contact_list: 'ContactList',
                          settings:     'Settings',
                          queues:       Dict[bytes, 'Queue']) -> None:
    """Generate new pre-shared key for manual key delivery.

    :param account:      The contact's account name (e.g. alice@jabber.org)
    :param user:         The user's account name (e.g. bob@jabber.org)
    :param nick:         Nick of contact
    :param contact_list: Contact list object
    :param settings:     Settings object
    :param queues:       Dictionary of multiprocessing queues
    :return:             None
    """
    try:
        tx_key   = csprng()
        tx_hek   = csprng()
        salt     = csprng()
        password = MasterKey.new_password("password for PSK")

        phase("Deriving key encryption key", head=2)
        kek, _ = argon2_kdf(password, salt, parallelism=1)
        phase(DONE)

        ct_tag = encrypt_and_sign(tx_key + tx_hek, key=kek)

        while True:
            store_d = ask_path_gui(f"Select removable media for {nick}", settings)
            f_name  = f"{store_d}/{user}.psk - Give to {account}"
            try:
                with open(f_name, 'wb+') as f:
                    f.write(salt + ct_tag)
                break
            except PermissionError:
                c_print("Error: Did not have permission to write to directory.")
                time.sleep(0.5)
                continue

        packet = KEY_EX_PSK_TX_HEADER \
                 + tx_key \
                 + tx_hek \
                 + account.encode() + US_BYTE + nick.encode()

        queue_command(packet, settings, queues[COMMAND_PACKET_QUEUE])

        contact_list.add_contact(account, user, nick,
                                 bytes(FINGERPRINT_LEN), bytes(FINGERPRINT_LEN),
                                 settings.log_messages_by_default,
                                 settings.accept_files_by_default,
                                 settings.show_notifications_by_default)

        queues[KEY_MANAGEMENT_QUEUE].put((KDB_ADD_ENTRY_HEADER, account,
                                          tx_key, csprng(),
                                          tx_hek, csprng()))

        box_print(f"Successfully added {nick}.", head=1)
        clear_screen(delay=1)

    except KeyboardInterrupt:
        raise FunctionReturn("PSK generation aborted.", delay=1, head=2, tail_clear=True)


def rxm_load_psk(window:       'TxWindow',
                 contact_list: 'ContactList',
                 settings:     'Settings',
                 c_queue:      'Queue') -> None:
    """Load PSK for selected contact on RxM."""
    if settings.session_traffic_masking:
        raise FunctionReturn("Error: Command is disabled during traffic masking.")

    if window.type == WIN_TYPE_GROUP:
        raise FunctionReturn("Error: Group is selected.")

    if contact_list.get_contact(window.uid).tx_fingerprint != bytes(FINGERPRINT_LEN):
        raise FunctionReturn("Error: Current key was exchanged with X25519.")

    packet = KEY_EX_PSK_RX_HEADER + window.uid.encode()
    queue_command(packet, settings, c_queue)

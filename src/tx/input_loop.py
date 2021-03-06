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
import readline
import sys
import typing

from typing import Dict

from src.common.exceptions import FunctionReturn
from src.common.misc       import get_tab_completer, ignored
from src.common.statics    import *

from src.tx.commands      import process_command
from src.tx.contact       import add_new_contact
from src.tx.key_exchanges import new_local_key
from src.tx.packet        import queue_file, queue_message
from src.tx.user_input    import get_input
from src.tx.windows       import TxWindow

if typing.TYPE_CHECKING:
    from multiprocessing         import Queue
    from src.common.db_contacts  import ContactList
    from src.common.db_groups    import GroupList
    from src.common.db_masterkey import MasterKey
    from src.common.db_settings  import Settings
    from src.common.gateway      import Gateway


def input_loop(queues:       Dict[bytes, 'Queue'],
               settings:     'Settings',
               gateway:      'Gateway',
               contact_list: 'ContactList',
               group_list:   'GroupList',
               master_key:   'MasterKey',
               stdin_fd:     int) -> None:
    """Get input from user and process it accordingly.

    Tx side of TFC runs two processes -- input and sender loop -- separate
    from one another. This allows prioritized output of queued assembly
    packets. input_loop handles Tx-side functions excluding assembly packet
    encryption, output and logging, and hash ratchet key/counter updates in
    key_list database.
    """
    sys.stdin = os.fdopen(stdin_fd)
    window    = TxWindow(contact_list, group_list)

    while True:
        with ignored(EOFError, FunctionReturn, KeyboardInterrupt):
            readline.set_completer(get_tab_completer(contact_list, group_list, settings))
            readline.parse_and_bind('tab: complete')

            window.update_group_win_members(group_list)

            while not contact_list.has_local_contact():
                new_local_key(contact_list, settings, queues)

            while not contact_list.has_contacts():
                add_new_contact(contact_list, group_list, settings, queues)

            while not window.is_selected():
                window.select_tx_window(settings, queues)

            user_input = get_input(window, settings)

            if user_input.type == MESSAGE:
                queue_message(user_input, window, settings, queues[MESSAGE_PACKET_QUEUE])

            elif user_input.type == FILE:
                queue_file(window, settings, queues[FILE_PACKET_QUEUE], gateway)

            elif user_input.type == COMMAND:
                process_command(user_input, window, settings, queues, contact_list, group_list, master_key)

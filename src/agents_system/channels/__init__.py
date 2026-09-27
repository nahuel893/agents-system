"""Messaging channels behind one port (#22).

``port`` defines what a channel must do -- authenticate an inbound delivery,
normalize its payload, send a reply -- and nothing else. ``whatsapp`` is the
first adapter behind it (Meta WhatsApp Cloud API).

Deliberately empty of imports: ``channels.whatsapp`` composes modules from
``agents_system.integration``, whose package ``__init__`` imports the webhook
route, which imports ``channels.port``. Importing the adapter from here would
turn that one-way edge into a cycle.
"""

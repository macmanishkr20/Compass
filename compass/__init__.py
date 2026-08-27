"""Compass.

Four sections, and a front door:

    compass/home      the chat you talk to — tool-free, its own store
    compass/code      the agent that works in a workspace — tools, routines
    compass/design    turning a described idea into a design, and exporting it
    compass/common    what more than one of them needs
    compass/api       the ASGI app, which mounts the four

A section owns its own features. Something shared lives in `common`, and what
counts as shared is settled by the imports rather than by argument: more than
one section reaches for it. Common does not reach back into a section, except
in the two places that exist to summarise all of them, which say so.
"""

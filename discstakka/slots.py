"""Carousel geometry.

Its own module because both the wire protocol and the catalogue need it, and
making the catalogue import the protocol to learn how many slots exist drags
hidapi into every database query.
"""

SLOT_MIN = 1
SLOT_MAX = 100
HOME = 0

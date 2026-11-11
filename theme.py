"""Colour tokens for the card renderer.

Theme v2: 1940s New York noir, Art Deco brasswork and absinthe-tinted shadow.
Brass and gold carry the structure, ivory and parchment carry the type, emerald
is the absinthe accent, oxblood marks loss. Everything the cards draw reads from
here, so the whole look can be retuned in one place.
"""

# backgrounds
INK_TOP = (10, 17, 14)
INK_BOTTOM = (4, 7, 6)
INK = (6, 10, 9)
PANEL = (13, 21, 18)
PANEL_2 = (18, 28, 24)
PANEL_3 = (24, 35, 30)

# structure
LINE = (38, 53, 45)
LINE_2 = (60, 81, 69)
LINE_3 = (86, 104, 89)

# type
TEXT = (237, 228, 210)      # ivory
PARCHMENT = (201, 188, 164)
MUTED = (154, 143, 122)
FAINT = (122, 112, 95)

# accents
GREEN = (79, 174, 131)      # absinthe emerald: win, vitality
EMERALD = GREEN
MINT = (143, 217, 188)      # souls (pairs with assets/souls.png)
BRASS = (185, 146, 79)      # structure, ranked, PP
GOLD = (216, 179, 106)      # bright rules and display accents
COPPER = (154, 95, 58)
EMBER = (217, 154, 78)      # brawl
AMBER = EMBER
RED = (201, 107, 94)        # readable oxblood for loss / deaths text
OXBLOOD = (168, 67, 58)     # fills and rules only (3.1:1 on panel)
RED_SOFT = (216, 139, 127)
STEEL = (126, 154, 168)     # bot matches
VIOLET = (142, 114, 176)    # spirit items

SLOT_COLORS = {"weapon": BRASS, "vitality": GREEN, "spirit": VIOLET}
SLOT_ORDER = {"weapon": 0, "vitality": 1, "spirit": 2}
MODE_COLORS = {"ranked": BRASS, "brawl": EMBER, "bot": STEEL, "standard": MUTED, "other": MUTED}

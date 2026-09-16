"""Host-side fan controller for Tyan FT77C-B7079 (S7079GM2NR-N).

Drives the ASPEED AST2400 BMC PWM/tach controller directly from the host via
the PCIe-to-AHB (P2A) bridge exposed by the ASPEED VGA function's BAR1.
"""

__version__ = "1.0.0"

"""Month-end derivative pricing automation platform.

Pipeline (see SYSTEMIMPLEMENTATION.md):
1. Collect inputs (broker statements, traders export, analyst market data)
2. Parse broker statements -> normalized BrokerMark records
3. Match marks to the live-trade population
4. Compute notionals / USD conversions / per-unit prices (rules F1-F5)
5. Reconcile derived vs broker MTM, write canonical CSV + exceptions
"""

__version__ = "0.1.0"
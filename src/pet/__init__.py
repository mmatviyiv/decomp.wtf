"""pet — macOS software reconstruction tracker.

A small, dependency-free layer on top of the decomp.wtf fork: Mach-O symbol
inventory, claim/release workflow, evidence and check tracking, and a stdio MCP
server. It deliberately avoids the ROM pipeline and the byte-for-byte judge:
macOS/Swift/ObjC reconstruction is verified by builds, tests and attached
Evidence, not by rebuilding the original bytes.
"""

__version__ = "0.1.0"

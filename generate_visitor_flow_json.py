from __future__ import annotations

from pathlib import Path

from visitor_flow_schema import write_flow_json


def main() -> None:
    here = Path(__file__).resolve().parent
    out_local = write_flow_json(here / "visitor_flow.json")
    out_prod = write_flow_json(here / "Production" / "visitor_flow.json")
    print(f"Wrote: {out_local}")
    print(f"Wrote: {out_prod}")


if __name__ == "__main__":
    main()


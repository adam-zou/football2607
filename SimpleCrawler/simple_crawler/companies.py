"""Titan007 company identities owned by the standalone crawler."""

from types import MappingProxyType
from typing import Mapping


COMPANY_NAMES: Mapping[int, str] = MappingProxyType(
    {
        3: "Crow*",
        4: "立*",
        8: "36*",
        24: "12*",
        31: "利*",
        47: "平*",
    }
)
COMPANY_IDS = tuple(COMPANY_NAMES)


def company_label(company_id: int) -> str:
    """Return a stable log label containing both company ID and name."""

    try:
        name = COMPANY_NAMES[company_id]
    except KeyError as error:
        raise ValueError(f"unsupported company_id: {company_id}") from error
    return f"公司 {company_id}（{name}）"

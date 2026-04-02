from dataclasses import dataclass


@dataclass
class ScoreResult:
    qualification_score: int
    qualification_flag: str


def classify_score(score: int, label: str) -> str:
    if score >= 5:
        return f"Strong {label}"
    if score >= 3:
        return f"Medium {label}"
    return "Manual Review Required"


def score_application(payload: dict) -> ScoreResult:
    address_complete = all(
        payload.get(field)
        for field in ["region", "zone", "woreda", "kebele", "village", "phone"]
    )

    sales_score = 0
    installer_score = 0

    if payload.get("experience"):
        sales_score += 2
        installer_score += 2

    if payload.get("has_shop"):
        sales_score += 2

    if address_complete:
        sales_score += 1

    if payload.get("territory_valid", True):
        sales_score += 1

    if payload.get("can_install"):
        installer_score += 3

    if payload.get("id_file_front_url") and payload.get("id_file_back_url"):
        installer_score += 1

    applicant_type = payload.get("applicant_type")
    if applicant_type == "sales_only":
        total = sales_score
        flag = classify_score(total, "Sales Candidate")
    elif applicant_type == "installer_only":
        total = installer_score
        flag = classify_score(total, "Installer Candidate")
    else:
        total = max(sales_score, installer_score)
        if total >= 5:
            flag = "Strong Hybrid Candidate"
        elif total >= 3:
            flag = "Medium Hybrid Candidate"
        else:
            flag = "Manual Review Required"

    return ScoreResult(qualification_score=total, qualification_flag=flag)

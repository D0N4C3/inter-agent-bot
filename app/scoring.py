from dataclasses import dataclass


@dataclass
class ScoreResult:
    qualification_score: int
    qualification_flag: str


def score_application(payload: dict) -> ScoreResult:
    address_complete = all(payload.get(field) for field in ["region", "zone", "woreda", "kebele", "village", "phone"])
    experience_years = int(payload.get("experience_years") or 0)

    sales_score = 0
    installer_score = 0

    if payload.get("experience"):
        sales_score += 25
        installer_score += 25

    if payload.get("has_shop"):
        sales_score += 25

    if address_complete:
        sales_score += 15

    if payload.get("territory_valid", True):
        sales_score += 20

    if "sales" in str(payload.get("work_type", "")).lower():
        sales_score += 15

    if payload.get("can_install"):
        installer_score += 35

    if payload.get("id_file_front_url") and payload.get("id_file_back_url"):
        installer_score += 20

    installer_score += min(experience_years * 4, 20)

    applicant_type = payload.get("applicant_type")

    if sales_score >= 70 and installer_score >= 70:
        flag = "Strong Hybrid"
    elif applicant_type == "sales_only" and sales_score >= 70:
        flag = "Strong Sales Candidate"
    elif applicant_type == "installer_only" and installer_score >= 70:
        flag = "Strong Installer Candidate"
    elif max(sales_score, installer_score) >= 50:
        flag = "Hybrid"
    else:
        flag = "Manual Review"

    total = max(sales_score, installer_score)
    return ScoreResult(qualification_score=total, qualification_flag=flag)

from dataclasses import dataclass


@dataclass
class ScoreResult:
    qualification_score: int
    qualification_flag: str


def score_application(payload: dict) -> ScoreResult:
    address_complete = all(payload.get(field) for field in ["region", "zone", "woreda", "preferred_territory", "phone"])
    experience_years = int(payload.get("experience_years") or 0)
    work_type = str(payload.get("work_type", "")).lower()

    sales_score = 0
    installer_score = 0

    if payload.get("experience"):
        sales_score += 20
        installer_score += 20

    if payload.get("has_shop"):
        sales_score += 25
        installer_score += 5

    if address_complete:
        sales_score += 15
        installer_score += 10

    if payload.get("territory_valid", True):
        sales_score += 10
        installer_score += 10

    if "sales" in work_type:
        sales_score += 20
    if "install" in work_type or "technician" in work_type:
        installer_score += 20

    if payload.get("can_install"):
        installer_score += 35

    if payload.get("id_file_front_url") and payload.get("id_file_back_url"):
        installer_score += 20
        sales_score += 10

    sales_score += min(experience_years * 3, 15)
    installer_score += min(experience_years * 4, 20)

    applicant_type = payload.get("applicant_type")

    if applicant_type == "sales_only":
        if sales_score >= 70:
            flag = "Strong Sales Candidate"
        else:
            flag = "Manual Review"
    elif applicant_type == "installer_only":
        if installer_score >= 70:
            flag = "Strong Installer Candidate"
        else:
            flag = "Manual Review"
    elif applicant_type == "sales_installer":
        if sales_score >= 75 and installer_score >= 75:
            flag = "Hybrid"
        elif max(sales_score, installer_score) >= 65:
            flag = "Hybrid"
        elif sales_score >= 70:
            flag = "Strong Sales Candidate"
        elif installer_score >= 70:
            flag = "Strong Installer Candidate"
        else:
            flag = "Manual Review"
    elif sales_score >= 70 and installer_score >= 70:
        flag = "Hybrid"
    elif sales_score >= 70:
        flag = "Strong Sales Candidate"
    elif installer_score >= 70:
        flag = "Strong Installer Candidate"
    else:
        flag = "Manual Review"

    total = max(sales_score, installer_score)
    return ScoreResult(qualification_score=total, qualification_flag=flag)

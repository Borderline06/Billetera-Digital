from pydantic import BaseModel, Field

class ExternalTransfer(BaseModel):
    from_user: int
    amount: float = Field(..., gt=0)
    to_account: str
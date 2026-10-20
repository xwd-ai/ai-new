from pydantic import BaseModel


class CreateTicketRequest(BaseModel):
    description: str
    tried_steps: str = ""
    evidence: str = ""


class ReplyRequest(BaseModel):
    ticket_id: str
    content: str

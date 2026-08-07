from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.schemas import (
    SessionDetailResponse,
    SessionListResponse,
    SessionSummaryResponse,
    SessionTurnResponse,
)
from app.deps import get_session_history_service
from app.history.service import SessionHistoryService


router = APIRouter(prefix="/api/sessions", tags=["consultation history"])


@router.get("", response_model=SessionListResponse)
def list_sessions(
    service: SessionHistoryService = Depends(get_session_history_service),
) -> SessionListResponse:
    return SessionListResponse(
        sessions=[
            SessionSummaryResponse.model_validate(item)
            for item in service.list_sessions()
        ]
    )


@router.get("/{session_id}", response_model=SessionDetailResponse)
def get_session(
    session_id: UUID,
    service: SessionHistoryService = Depends(get_session_history_service),
) -> SessionDetailResponse:
    detail = service.get_session(str(session_id))
    return SessionDetailResponse(
        session=SessionSummaryResponse.model_validate(detail.session),
        turns=[
            SessionTurnResponse.model_validate(turn)
            for turn in detail.turns
        ],
    )


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_session(
    session_id: UUID,
    service: SessionHistoryService = Depends(get_session_history_service),
) -> Response:
    service.delete_session(str(session_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)

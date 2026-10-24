"""FSM (Finite State Machine) states for conversational user input without slash commands."""

from aiogram.fsm.state import State, StatesGroup


class TargetStates(StatesGroup):
    waiting_for_target_input = State()


class LookupStates(StatesGroup):
    waiting_for_user_input = State()
    waiting_for_mode = State()


class ThresholdStates(StatesGroup):
    waiting_for_threshold = State()

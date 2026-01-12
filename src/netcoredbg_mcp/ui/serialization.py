"""Serialization utilities for UI elements."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pywinauto.base_wrapper import BaseWrapper

logger = logging.getLogger(__name__)


@dataclass
class ElementInfo:
    """Information about a UI element."""

    automation_id: str
    control_type: str
    name: str
    class_name: str
    rectangle: dict[str, int]
    is_enabled: bool
    is_visible: bool
    has_keyboard_focus: bool
    child_count: int
    children: list[ElementInfo]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary with camelCase keys for JSON serialization."""
        return {
            "automationId": self.automation_id,
            "controlType": self.control_type,
            "name": self.name,
            "className": self.class_name,
            "rectangle": self.rectangle,
            "isEnabled": self.is_enabled,
            "isVisible": self.is_visible,
            "hasKeyboardFocus": self.has_keyboard_focus,
            "childCount": self.child_count,
            "children": [child.to_dict() for child in self.children],
        }


def serialize_element(
    element: BaseWrapper,
    max_depth: int = 3,
    max_children: int = 50,
    current_depth: int = 0,
) -> ElementInfo:
    """
    Serialize a pywinauto element to ElementInfo.

    Args:
        element: The pywinauto element wrapper to serialize
        max_depth: Maximum depth to traverse in the element tree
        max_children: Maximum number of children to serialize per element
        current_depth: Current depth in the tree (internal use)

    Returns:
        ElementInfo object containing the element's properties. If serialization
        fails for any reason (e.g., stale element), a minimal ElementInfo object
        with default values and an error name is returned.
    """
    try:
        # Get basic properties
        automation_id = ""
        control_type = ""
        name = ""
        class_name = ""
        rectangle = {"left": 0, "top": 0, "right": 0, "bottom": 0}
        is_enabled = False
        is_visible = False
        has_keyboard_focus = False

        # Try to get automation ID
        try:
            automation_id = element.element_info.automation_id or ""
        except (AttributeError, Exception) as e:
            logger.debug(f"Could not get automation_id: {e}")

        # Try to get control type
        try:
            control_type = element.element_info.control_type or ""
        except (AttributeError, Exception) as e:
            logger.debug(f"Could not get control_type: {e}")

        # Try to get name
        try:
            name = element.element_info.name or ""
        except (AttributeError, Exception) as e:
            logger.debug(f"Could not get name: {e}")

        # Try to get class name
        try:
            class_name = element.element_info.class_name or ""
        except (AttributeError, Exception) as e:
            logger.debug(f"Could not get class_name: {e}")

        # Try to get rectangle
        try:
            rect = element.rectangle()
            rectangle = {
                "left": rect.left,
                "top": rect.top,
                "right": rect.right,
                "bottom": rect.bottom,
            }
        except (AttributeError, Exception) as e:
            logger.debug(f"Could not get rectangle: {e}")

        # Try to get enabled state
        try:
            is_enabled = element.is_enabled()
        except (AttributeError, Exception) as e:
            logger.debug(f"Could not get is_enabled: {e}")

        # Try to get visible state
        try:
            is_visible = element.is_visible()
        except (AttributeError, Exception) as e:
            logger.debug(f"Could not get is_visible: {e}")

        # Try to get keyboard focus state
        try:
            has_keyboard_focus = element.has_keyboard_focus()
        except (AttributeError, Exception) as e:
            logger.debug(f"Could not get has_keyboard_focus: {e}")

        # Get children if we haven't reached max depth
        children: list[ElementInfo] = []
        child_count = 0

        if current_depth < max_depth:
            try:
                child_elements = element.children()
                child_count = len(child_elements)

                # Serialize children up to max_children limit
                for i, child in enumerate(child_elements):
                    if i >= max_children:
                        logger.debug(
                            f"Reached max_children limit ({max_children}), "
                            f"skipping remaining {child_count - max_children} children"
                        )
                        break

                    try:
                        child_info = serialize_element(
                            child,
                            max_depth=max_depth,
                            max_children=max_children,
                            current_depth=current_depth + 1,
                        )
                        children.append(child_info)
                    except Exception as e:
                        logger.debug(f"Could not serialize child {i}: {e}")
                        continue

            except (AttributeError, Exception) as e:
                logger.debug(f"Could not get children: {e}")

        return ElementInfo(
            automation_id=automation_id,
            control_type=control_type,
            name=name,
            class_name=class_name,
            rectangle=rectangle,
            is_enabled=is_enabled,
            is_visible=is_visible,
            has_keyboard_focus=has_keyboard_focus,
            child_count=child_count,
            children=children,
        )

    except Exception as e:
        logger.error(f"Error serializing element: {e}")
        # Return a minimal ElementInfo if we can't get any properties
        return ElementInfo(
            automation_id="",
            control_type="",
            name="<error>",
            class_name="",
            rectangle={"left": 0, "top": 0, "right": 0, "bottom": 0},
            is_enabled=False,
            is_visible=False,
            has_keyboard_focus=False,
            child_count=0,
            children=[],
        )

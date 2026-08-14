import logging
from json import JSONDecodeError

from aiohttp import web

from ledfx.api import RestEndpoint
from ledfx.api.virtual_effects import process_fallback
from ledfx.color import (
    build_gradient_config,
    validate_color,
    validate_gradient,
)
from ledfx.config import save_config
from ledfx.virtuals import Virtuals, apply_config_to_active_effects

_LOGGER = logging.getLogger(__name__)


class EffectsEndpoint(RestEndpoint):
    ENDPOINT_PATH = "/api/effects"

    async def get(self) -> web.Response:
        """
        Retrieves the active effects for each virtual LED strip.

        Each entry also reports ``color_override``: the transient colour or
        gradient currently recolouring that effect, or ``None``.

        Returns:
            web.Response: The HTTP response containing the active effects for each virtual LED strip.
        """
        response = {"status": "success", "effects": {}}
        for virtual in self._ledfx.virtuals.values():
            if virtual.active_effect:
                response["effects"][virtual.id] = {
                    "effect_type": virtual.active_effect.type,
                    "effect_config": virtual.active_effect.config,
                    "color_override": virtual.color_override,
                }
        return await self.bare_request_success(response)

    async def put(self, request: web.Request) -> web.Response:
        """
        Handle PUT request to clear all effects on all devices.

        Args:
            request (web.Request): The request including the `action` to perform.

        Returns:
            web.Response: The HTTP response object.
        """
        try:
            data = await request.json()
        except JSONDecodeError:
            return await self.json_decode_error()

        action = data.get("action")
        if action is None:
            return await self.invalid_request(
                'Required attribute "action" was not provided'
            )

        if action not in [
            "clear_all_effects",
            "apply_global",
            "apply_global_effect",
            "apply_override",
            "clear_override",
        ]:
            return await self.invalid_request(f'Invalid action "{action}"')

        if action == "apply_global":
            return await self._apply_global(data)

        if action == "apply_global_effect":
            return await self._apply_global_effect(data)

        if action == "apply_override":
            return await self._apply_override(data)

        if action == "clear_override":
            return await self._clear_override(data)

        # Clear all effects on all devices
        if action == "clear_all_effects":
            self._ledfx.virtuals.clear_all_effects()
            return await self.request_success(
                "info", "Cleared all effects on all devices"
            )

    async def _apply_global(self, data: dict) -> web.Response:
        """
        Apply a global configuration update across active effects.

        This was extracted from the PUT handler to keep that method tidy
        while preserving the original behavior.
        """
        # Define supported configuration keys and their validation
        SUPPORTED_KEYS = {
            "gradient": {"validator": validate_gradient, "type": "string"},
            "background_color": {
                "validator": validate_color,
                "type": "string",
            },
            "background_brightness": {
                "validator": lambda x: max(0.0, min(1.0, float(x))),
                "type": "number",
            },
            "brightness": {
                "validator": lambda x: max(0.0, min(1.0, float(x))),
                "type": "number",
            },
            "flip": {"validator": None, "type": "boolean"},
            "mirror": {"validator": None, "type": "boolean"},
        }

        # Check if at least one supported key is provided
        provided_keys = [key for key in SUPPORTED_KEYS.keys() if key in data]
        if not provided_keys:
            return await self.invalid_request(
                f'At least one of the following attributes must be provided: {", ".join(SUPPORTED_KEYS.keys())}'
            )

        # Validate and process each provided key
        config_updates = {}

        for key in provided_keys:
            value = data[key]
            key_info = SUPPORTED_KEYS[key]

            try:
                if key == "gradient":
                    try:
                        gradient_config = build_gradient_config(
                            value,
                            self._ledfx.gradients,
                            skip_keys=set(provided_keys),
                        )
                        config_updates.update(gradient_config)
                    except Exception as e:
                        return await self.invalid_request(
                            f'Invalid value for "{key}": {e}'
                        )

                elif key_info["type"] == "boolean":
                    # Special handling for boolean keys (True, False, "toggle")
                    if isinstance(value, bool):
                        config_updates[key] = value
                    elif isinstance(value, str) and value.lower() == "toggle":
                        # Mark for toggling - will be resolved per effect
                        config_updates[key] = "toggle"
                    else:
                        return await self.invalid_request(
                            f'Invalid value for "{key}": must be true, false, or "toggle"'
                        )

                else:
                    # Standard validation
                    if key_info["validator"]:
                        validated_value = key_info["validator"](value)
                        config_updates[key] = validated_value
                    else:
                        config_updates[key] = value

            except Exception as e:
                return await self.invalid_request(
                    f'Invalid value for "{key}": {e}'
                )

        # Optional filter: a list of virtual ids to restrict the update to
        virtuals_filter = None
        if "virtuals" in data:
            vlist = data["virtuals"]
            if not isinstance(vlist, list):
                return await self.invalid_request(
                    'Invalid value for "virtuals": must be a list of virtual ids'
                )
            virtuals_filter = {str(v) for v in vlist}

        updated, skipped = apply_config_to_active_effects(
            self._ledfx.virtuals.values(),
            config_updates,
            target_ids=virtuals_filter,
        )

        # Persist configuration changes
        if updated > 0:
            try:
                save_config(
                    config=self._ledfx.config,
                    config_dir=self._ledfx.config_dir,
                )
            except Exception as e:
                _LOGGER.warning(
                    "Failed to save config after apply_global: %s", e
                )

        return await self.request_success(
            "success",
            f"Applied global configuration to {updated} effects (skipped {skipped})",
        )

    def _resolve_virtuals(self, data: dict):
        """Resolve the optional ``virtuals`` filter of an override request.

        Args:
            data (dict): the request payload.

        Returns:
            tuple: ``(virtuals, error)``. ``virtuals`` is the list of
            :class:`~ledfx.virtuals.Virtual` to act on - every virtual when no
            filter was supplied - and ``error`` is a message string when the
            filter was malformed, otherwise ``None``.
        """
        if "virtuals" not in data:
            return list(self._ledfx.virtuals.values()), None

        vlist = data["virtuals"]
        if not isinstance(vlist, list):
            return (
                None,
                'Invalid value for "virtuals": must be a list of virtual ids',
            )

        target_ids = {str(v) for v in vlist}
        return [
            virtual
            for virtual in self._ledfx.virtuals.values()
            if virtual.id in target_ids
        ], None

    async def _apply_override(self, data: dict) -> web.Response:
        """Apply a transient colour override to the targeted virtuals.

        Unlike ``apply_global``, this does not touch effect config and is not
        persisted: the running effect keeps animating underneath and
        ``clear_override`` restores it exactly. Effects without a ``gradient``
        config key are recoloured too, which ``apply_global`` cannot do.

        Expected payload::

            {
                "action": "apply_override",
                "color": "#ff0000",          # or "gradient": "linear-gradient(...)"
                "virtuals": ["id1", "id2"]   # optional, defaults to all
            }

        Args:
            data (dict): the request payload.

        Returns:
            web.Response: the HTTP response object.
        """
        if ("color" in data) == ("gradient" in data):
            return await self.invalid_request(
                'Exactly one of "color" or "gradient" must be provided'
            )

        try:
            if "color" in data:
                override = validate_color(data["color"])
            else:
                override = validate_gradient(data["gradient"])
        except Exception as e:
            return await self.invalid_request(f"Invalid override color: {e}")

        virtuals, error = self._resolve_virtuals(data)
        if error is not None:
            return await self.invalid_request(error)

        for virtual in virtuals:
            virtual.set_color_override(override)

        return await self.request_success(
            "success",
            f"Applied color override to {len(virtuals)} virtuals",
        )

    async def _clear_override(self, data: dict) -> web.Response:
        """Clear the colour override on the targeted virtuals.

        Expected payload::

            {
                "action": "clear_override",
                "virtuals": ["id1", "id2"]   # optional, defaults to all
            }

        Args:
            data (dict): the request payload.

        Returns:
            web.Response: the HTTP response object.
        """
        virtuals, error = self._resolve_virtuals(data)
        if error is not None:
            return await self.invalid_request(error)

        cleared = 0
        for virtual in virtuals:
            if virtual.color_override is not None:
                virtual.clear_color_override()
                cleared += 1

        return await self.request_success(
            "success",
            f"Cleared color override on {cleared} virtuals",
        )

    async def _apply_global_effect(self, data: dict) -> web.Response:
        """
        Apply a specific effect (type + config) to a list of virtual ids.

        Expected payload:
        {
            "virtuals": ["id1", "id2", ...],
            "type": "effect_type",
            "config": { ... }    # optional, empty dict resets
        }
        """

        vlist = data.get("virtuals", None)
        if vlist is None:
            vlist = Virtuals.get_virtual_ids()
        elif not isinstance(vlist, list) or not vlist:
            return await self.invalid_request(
                'Invalid value for "virtuals": must be a non-empty list of virtual ids'
            )

        effect_type = data.get("type")
        if not effect_type:
            return await self.invalid_request(
                'Required attribute "type" was not provided'
            )

        # Effect config may be omitted (treated as reset) or provided as a dict
        effect_config = data.get("config")
        if effect_config == "RANDOMIZE":
            return await self.invalid_request(
                "RANDOMIZE is not supported for apply_global_effect"
            )
        if effect_config is None:
            # Reset behavior
            effect_config = {}

        # Fallback behaviour (same semantics as virtual endpoint)
        fallback = process_fallback(data.get("fallback", None))

        applied = 0
        skipped = 0
        blocked = 0
        failed = 0

        for vid in vlist:
            virtual = self._ledfx.virtuals.get(str(vid))
            if virtual is None:
                skipped += 1
                continue

            if fallback is not None and virtual.streaming:
                # Don't interrupt the whole operation; record that this virtual is
                # blocked due to an active stream and skip it.
                blocked += 1
                _LOGGER.debug(
                    "Skipping virtual %s: streaming active and fallback provided",
                    vid,
                )
                continue

            # Create the effect and set it on the virtual. If config is empty, this
            # effectively resets to defaults (effects.create will handle defaulting).
            try:
                effect = self._ledfx.effects.create(
                    ledfx=self._ledfx, type=effect_type, config=effect_config
                )
                # apply effect with provided fallback (may be None)
                virtual.set_effect(effect, fallback=fallback)
                virtual.update_effect_config(effect)
                applied += 1
            except (ValueError, RuntimeError) as msg:
                _LOGGER.warning(
                    "Unable to set effect on virtual %s: %s", vid, msg
                )
                failed += 1

        # Persist configuration changes if anything applied
        if applied > 0:
            try:
                save_config(
                    config=self._ledfx.config,
                    config_dir=self._ledfx.config_dir,
                )
            except Exception as e:
                _LOGGER.warning(
                    "Failed to save config after apply_global_effect: %s", e
                )

        return await self.request_success(
            "success",
            f"Applied effect '{effect_type}' to {applied} virtuals (skipped {skipped}, blocked {blocked}, failed {failed})",
        )

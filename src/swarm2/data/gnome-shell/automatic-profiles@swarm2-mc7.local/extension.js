import Gio from 'gi://Gio';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const DBUS_NAME = 'io.github.swarm2mc7.ForegroundApplication';
const DBUS_OBJECT_PATH = '/io/github/swarm2mc7/ForegroundApplication';
const DBUS_INTERFACE = 'io.github.swarm2mc7.ForegroundApplication';

const DBUS_XML = `
<node>
  <interface name="${DBUS_INTERFACE}">
    <method name="GetFocusedWindowPid">
      <arg name="has_focused_window" type="b" direction="out"/>
      <arg name="pid" type="u" direction="out"/>
    </method>
  </interface>
</node>`;

class FocusedWindowService {
    GetFocusedWindowPid() {
        const window = global.display.focus_window;
        if (window === null)
            return [false, 0];

        const pid = window.get_pid();
        if (!Number.isInteger(pid) || pid <= 0 || pid > 0xffffffff)
            return [true, 0];
        return [true, pid];
    }
}

export default class AutomaticProfilesExtension extends Extension {
    enable() {
        const service = new FocusedWindowService();
        const exportedObject = Gio.DBusExportedObject.wrapJSObject(DBUS_XML, service);
        this._exportedObject = exportedObject;
        this._isExported = false;
        this._ownerId = Gio.bus_own_name(
            Gio.BusType.SESSION,
            DBUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            connection => {
                if (this._exportedObject !== exportedObject)
                    return;
                exportedObject.export(connection, DBUS_OBJECT_PATH);
                this._isExported = true;
            },
            null,
            null
        );
    }

    disable() {
        if (this._isExported)
            this._exportedObject.unexport();
        if (this._ownerId)
            Gio.bus_unown_name(this._ownerId);
        this._exportedObject = null;
        this._isExported = false;
        this._ownerId = 0;
    }
}

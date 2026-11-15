package cn.campusdesk.calendar;

import android.content.ContentProviderOperation;
import android.content.ContentResolver;
import android.content.ContentUris;
import android.content.ContentValues;
import android.content.OperationApplicationException;
import android.database.Cursor;
import android.net.Uri;
import android.os.RemoteException;
import android.provider.CalendarContract;
import android.provider.CalendarContract.Calendars;
import android.provider.CalendarContract.Events;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.Map;
import java.util.Objects;
import java.util.function.BooleanSupplier;

final class CalendarStore {
    private static final String NAME = "中传校园日程";
    private static final String ACCOUNT = "cn.campusdesk.calendar";
    private static final String LOCAL = CalendarContract.ACCOUNT_TYPE_LOCAL;
    private static final Uri CALENDARS = syncUri(Calendars.CONTENT_URI);
    private static final Uri EVENTS = syncUri(Events.CONTENT_URI);
    private static final String[] EVENT_FIELDS = {
        Events._ID, Events._SYNC_ID, Events.TITLE, Events.DTSTART, Events.DTEND, Events.EVENT_LOCATION, Events.DESCRIPTION
    };

    private final ContentResolver provider;

    CalendarStore(ContentResolver provider) {
        this.provider = provider;
    }

    long find() {
        String filter = Calendars.ACCOUNT_NAME + "=? AND " + Calendars.ACCOUNT_TYPE + "=? AND " + Calendars.NAME + "=?";
        try (Cursor row = provider.query(CALENDARS, new String[]{Calendars._ID}, filter,
                new String[]{ACCOUNT, LOCAL, ACCOUNT}, null)) {
            if (row == null) throw new IllegalStateException("calendar provider unavailable");
            return row.moveToFirst() ? row.getLong(0) : -1;
        }
    }

    void remove() {
        long id = find();
        if (id >= 0) provider.delete(ContentUris.withAppendedId(CALENDARS, id), null, null);
    }

    void write(long id, Map<String, CalendarFeed.Event> events, BooleanSupplier canceled)
            throws RemoteException, OperationApplicationException, CalendarSync.Failure {
        CalendarSync.checkCanceled(canceled);
        if (id < 0) id = create();
        ArrayList<Long> stale = new ArrayList<>();
        ArrayList<ContentProviderOperation> changes = new ArrayList<>();
        String filter = Events.CALENDAR_ID + "=? AND " + Events.DELETED + "=0 AND " + Events._SYNC_ID + " IS NOT NULL";
        try (Cursor row = provider.query(EVENTS, EVENT_FIELDS, filter, new String[]{Long.toString(id)}, null)) {
            if (row == null) throw new IllegalStateException("calendar events unavailable");
            Map<String, CalendarFeed.Event> unseen = new HashMap<>(events);
            while (row.moveToNext()) {
                long eventId = row.getLong(0);
                CalendarFeed.Event entry = unseen.remove(row.getString(1));
                if (entry == null) stale.add(eventId);
                else if (!matches(row, entry)) {
                    changes.add(ContentProviderOperation.newUpdate(ContentUris.withAppendedId(EVENTS, eventId))
                        .withValues(values(entry)).withExpectedCount(1).build());
                }
            }
            for (CalendarFeed.Event entry : unseen.values()) {
                changes.add(ContentProviderOperation.newInsert(EVENTS).withValues(values(entry))
                    .withValue(Events.CALENDAR_ID, id).build());
            }
        }
        apply(changes, canceled);
        changes.clear();
        for (Long eventId : stale) {
            changes.add(ContentProviderOperation.newDelete(ContentUris.withAppendedId(EVENTS, eventId)).build());
        }
        apply(changes, canceled);
    }

    private void apply(ArrayList<ContentProviderOperation> changes, BooleanSupplier canceled)
            throws RemoteException, OperationApplicationException, CalendarSync.Failure {
        for (int start = 0; start < changes.size(); start += 40) {
            CalendarSync.checkCanceled(canceled);
            provider.applyBatch(CalendarContract.AUTHORITY,
                new ArrayList<>(changes.subList(start, Math.min(start + 40, changes.size()))));
        }
    }

    private long create() {
        ContentValues values = new ContentValues();
        values.put(Calendars.ACCOUNT_NAME, ACCOUNT);
        values.put(Calendars.ACCOUNT_TYPE, LOCAL);
        values.put(Calendars.OWNER_ACCOUNT, ACCOUNT);
        values.put(Calendars.NAME, ACCOUNT);
        values.put(Calendars.CALENDAR_DISPLAY_NAME, NAME);
        values.put(Calendars.CALENDAR_COLOR, 0xff347759);
        values.put(Calendars.CALENDAR_TIME_ZONE, "Asia/Shanghai");
        values.put(Calendars.CALENDAR_ACCESS_LEVEL, Calendars.CAL_ACCESS_READ);
        values.put(Calendars.VISIBLE, 1);
        values.put(Calendars.SYNC_EVENTS, 1);
        Uri created = provider.insert(CALENDARS, values);
        if (created == null) throw new IllegalStateException("calendar creation failed");
        return ContentUris.parseId(created);
    }

    private static boolean matches(Cursor row, CalendarFeed.Event event) {
        return Objects.equals(row.getString(2), event.title) && row.getLong(3) == event.start && row.getLong(4) == event.end
            && Objects.equals(row.getString(5), event.location) && Objects.equals(row.getString(6), event.category);
    }

    private static ContentValues values(CalendarFeed.Event event) {
        ContentValues values = new ContentValues();
        values.put(Events._SYNC_ID, event.uid);
        values.put(Events.TITLE, event.title);
        values.put(Events.DTSTART, event.start);
        values.put(Events.DTEND, event.end);
        values.put(Events.EVENT_TIMEZONE, "Asia/Shanghai");
        values.put(Events.EVENT_LOCATION, event.location);
        values.put(Events.DESCRIPTION, event.category);
        values.put(Events.ACCESS_LEVEL, Events.ACCESS_PRIVATE);
        values.put(Events.AVAILABILITY, Events.AVAILABILITY_BUSY);
        return values;
    }

    private static Uri syncUri(Uri uri) {
        return uri.buildUpon().appendQueryParameter(CalendarContract.CALLER_IS_SYNCADAPTER, "true")
            .appendQueryParameter(Calendars.ACCOUNT_NAME, ACCOUNT).appendQueryParameter(Calendars.ACCOUNT_TYPE, LOCAL).build();
    }
}

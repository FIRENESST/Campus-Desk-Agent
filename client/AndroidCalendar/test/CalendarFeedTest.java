package cn.campusdesk.calendar;

import java.io.IOException;
import java.time.Instant;
import java.util.Map;

public final class CalendarFeedTest {
    private static final String START = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        + "BEGIN:VTIMEZONE\r\nTZID:Asia/Shanghai\r\nBEGIN:STANDARD\r\n"
        + "DTSTART:19920101T000000\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n";
    private static final String END = "END:VCALENDAR\r\n";
    private static final String EVENT = "BEGIN:VEVENT\r\nUID:lesson@cuc-campus-desk\r\n"
        + "DTSTART:20261116T000000Z\r\nDTEND:20261116T014000Z\r\n"
        + "SUMMARY:影像语言\\, 导\r\n 论\\n分组讨论\r\nLOCATION:A101\r\nCATEGORIES:课程\r\nEND:VEVENT\r\n";

    public static void main(String[] args) throws IOException {
        Map<String, CalendarFeed.Event> current = CalendarFeed.parse(START + EVENT + END);
        CalendarFeed.Event lesson = current.get("lesson@cuc-campus-desk");
        check(current.size() == 1 && lesson != null && "影像语言, 导论\n分组讨论".equals(lesson.title));
        check("A101".equals(lesson.location) && "课程".equals(lesson.category));
        check(lesson.start == Instant.parse("2026-11-16T00:00:00Z").toEpochMilli());
        check(lesson.end == Instant.parse("2026-11-16T01:40:00Z").toEpochMilli());
        check(CalendarFeed.parse(START + END).isEmpty());
        invalid(() -> CalendarFeed.parse(START + EVENT));
        invalid(() -> CalendarFeed.parse(START + EVENT + EVENT + END));
        invalid(() -> CalendarFeed.parse(START + EVENT.replace("20261116T014000Z", "20261115T014000Z") + END));

        String path = "/calendar/cal_" + "A".repeat(43) + ".ics";
        check(CalendarFeed.link("https://campus.example.test:8443" + path).getPort() == 8443);
        invalid(() -> CalendarFeed.link("http://campus.example.test" + path));
        invalid(() -> CalendarFeed.link("https://campus.example.test" + path + "?redirect=true"));
        invalid(() -> CalendarFeed.link("https://campus.example.test/downloads/campus-calendar.apk"));
        System.out.println("Calendar feed parser and subscription URL checks passed");
    }

    private static void check(boolean valid) {
        if (!valid) throw new AssertionError("Unexpected calendar result");
    }

    private interface Checked { void run() throws IOException; }

    private static void invalid(Checked action) throws IOException {
        try { action.run(); }
        catch (IOException expected) { return; }
        throw new AssertionError("Invalid calendar input was accepted");
    }
}

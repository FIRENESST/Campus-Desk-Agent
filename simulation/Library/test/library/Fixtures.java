package library;

import java.nio.file.Path;
import java.time.Clock;
import java.util.List;

final class Fixtures {
    static LibraryStore store() {
        return store(null, Clock.systemUTC());
    }

    static LibraryStore store(Path state, Clock clock) {
        return new LibraryStore(state, clock, List.of(
                title("b-001", LibraryCatalog.Category.LITERATURE, 2),
                title("b-002", LibraryCatalog.Category.LITERATURE, 2),
                title("b-003", LibraryCatalog.Category.LITERATURE, 1),
                title("b-004", LibraryCatalog.Category.HISTORY, 3),
                title("b-005", LibraryCatalog.Category.HISTORY, 2),
                title("b-006", LibraryCatalog.Category.SCIENCE, 2),
                title("b-007", LibraryCatalog.Category.SOCIAL_SCIENCE, 2),
                title("b-008", LibraryCatalog.Category.PHILOSOPHY, 2),
                title("b-009", LibraryCatalog.Category.ARTS, 2),
                title("b-010", LibraryCatalog.Category.MEDIA, 2),
                title("b-025", LibraryCatalog.Category.COMPUTING, 4)));
    }

    private static LibraryCatalog.Title title(String id, LibraryCatalog.Category category, int copies) {
        return new LibraryCatalog.Title(id, "测试图书 " + id, "测试作者", category, copies);
    }
}

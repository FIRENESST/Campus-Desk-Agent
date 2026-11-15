package library;

import java.io.IOException;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.List;
import java.util.Map;

final class LibraryCatalog {
    enum Category {
        LITERATURE("literature", "文学与小说"),
        HISTORY("history", "历史与传记"),
        SCIENCE("science", "科普与自然"),
        COMPUTING("computing", "计算机与技术"),
        SOCIAL_SCIENCE("social-science", "社会与经济"),
        PHILOSOPHY("philosophy", "哲学与心理"),
        ARTS("arts", "艺术与设计"),
        MEDIA("media", "传播与新闻");

        final String id;
        final String label;

        Category(String id, String label) {
            this.id = id;
            this.label = label;
        }
    }

    record Title(String id, String title, String author, Category category, int copies) {}

    static List<Title> load(String path) throws IOException {
        if (path == null || path.isBlank()) {
            return List.of();
        }
        Path file = Path.of(path);
        if (Files.size(file) > 8 * 1024 * 1024) {
            throw new IllegalArgumentException("馆藏配置文件过大");
        }
        if (!(Json.parse(Files.readString(file)) instanceof List<?> rows)) {
            throw new IllegalArgumentException("馆藏配置必须是 JSON 数组");
        }
        var ids = new HashSet<String>();
        var titles = new ArrayList<Title>();
        for (Object row : rows) {
            if (!(row instanceof Map<?, ?> data)
                    || !(data.get("id") instanceof String id) || id.isBlank() || id.length() > 64 || !ids.add(id)
                    || !(data.get("title") instanceof String title) || title.isBlank()
                    || !(data.get("author") instanceof String author) || author.isBlank()
                    || !(data.get("category") instanceof String category) || !(data.get("copies") instanceof BigDecimal count)) {
                throw new IllegalArgumentException("馆藏条目无效或 ID 重复");
            }
            int copies = count.intValueExact();
            if (copies < 1 || copies > 10000) {
                throw new IllegalArgumentException("图书份数必须为 1 到 10000");
            }
            Category type = Arrays.stream(Category.values()).filter(value -> value.id.equals(category)).findFirst()
                    .orElseThrow(() -> new IllegalArgumentException("未知馆藏分类"));
            titles.add(new Title(id, title, author, type, copies));
        }
        return List.copyOf(titles);
    }
}

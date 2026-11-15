# 校园导航数据接口

源码仓库不提供地图、路线、照片或街景点位。部署时将 `CAMPUS_NAVIGATION_DIR` 设置为已授权资料的本地目录，MainAgent 和 UserWebsite 使用同一目录。未设置时查找被 Git 忽略的 `DataBase/CampusNavigation/local/`；没有资料时导航会显示不可用，不影响聊天和其他服务。

| 文件 | 格式 |
| --- | --- |
| `base_map.geojson` | WGS84 GeoJSON FeatureCollection，`metadata.campus_id` 为校区标识；校区边界的 `properties.kind` 为 `campus_boundary`，道路、建筑和地标各用自己的类型 |
| `street_view.json` | `campus_id`、`campus_name`、`points`、可选 `routes`；点位经纬度按 `[longitude, latitude]` 排列 |
| `walking_routes.json` | 同一 `campus_id`、`road_names`、`connections`、`places`，可选 `replaced_edges`；地点的 `node` 对应可步行道路端点 |
| `media/` | 街景清单显式引用的本地媒体，仅部署者自己导入 |

可在 [街景模型](../../server/MainAgent/main_agent/campus_cluster/subskills/campus_street_view.py)、[步行路网实现](../../server/MainAgent/main_agent/campus_cluster/subskills/walking_routes.py) 与 [测试夹具](../../server/MainAgent/tests/navigation_fixture.py) 核对字段。测试夹具只在临时目录生成虚构几何，服务不会载入。

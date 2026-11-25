// MI_DoorA evaluates its mask and tangent normal after texture filtering.
#include <mitsuba/core/properties.h>
#include <mitsuba/render/texture.h>
#include <array>
#include <cmath>

NAMESPACE_BEGIN(mitsuba)

using RGB = Color<float, 3>;
using ScalarTexture = Texture<float, RGB>;

class DoorTexture final : public ScalarTexture {
public:
    explicit DoorTexture(const Properties &p) : ScalarTexture(p),
        m_image(p.get_texture<ScalarTexture>("image")) {
        if (p.has_property("mask")) {
            m_mask = p.get_texture<ScalarTexture>("mask");
            for (size_t i = 0; i < m_colors.size(); ++i)
                m_colors[i] = p.get<ScalarColor3f>("color" + std::to_string(i));
        }
    }

    RGB eval_3(const SurfaceInteraction3f &si, bool active) const override {
        RGB sample = m_image->eval_3(si, active);
        if (m_mask) {
            RGB mask = m_mask->eval_3(si, active);
            RGB tint = m_colors[3];
            for (size_t i = 0; i < 3; ++i)
                tint = dr::lerp(tint, m_colors[i], mask[i]);
            return sample * tint;
        }
        float x = 2.f * sample.x() - 1.f, y = 2.f * sample.y() - 1.f;
        float z = std::sqrt(std::max(0.f, 1.f - x*x - y*y));
        return .5f * (RGB(x, -y, z) + 1.f);
    }

    RGB eval(const SurfaceInteraction3f &si, bool active) const override {
        return eval_3(si, active);
    }

    float eval_1(const SurfaceInteraction3f &si, bool active) const override {
        return luminance(eval_3(si, active));
    }

    bool is_spatially_varying() const override { return true; }
    MI_DECLARE_CLASS(DoorTexture)

private:
    ref<ScalarTexture> m_image, m_mask;
    std::array<RGB, 4> m_colors;
};

extern "C" MI_EXPORT void init_plugin(std::string_view name, PluginRegisterFn register_plugin) {
    register_plugin(name, "scalar_rgb", ScalarTexture::Type,
                    [](void *, const Properties &p) -> ref<Object> { return new DoorTexture(p); });
}
NAMESPACE_END(mitsuba)

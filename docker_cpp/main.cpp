// main.cpp — 纯标准库图像处理: PGM(P5) 读取 → 直方图 → Otsu 自适应阈值 → 二值化输出
// 用途: C++ 能力验证(指针/内存管理/STL/RAII/面向对象), 在 gcc 容器内编译并用 gdb 调试。
// 依赖: 无(不引 OpenCV, 便于任意 Linux 容器编译)
// 用法: ./imgbin <input.pgm> <output.pgm>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <memory>
#include <numeric>
#include <stdexcept>
#include <string>
#include <vector>

// ---------- PGM 读取器: RAII 管理文件资源, 异常安全 ----------
class PgmImage {
public:
    int width = 0, height = 0;

    static PgmImage load(const std::string& path) {
        std::ifstream f(path, std::ios::binary);
        if (!f) throw std::runtime_error("无法打开: " + path);
        std::string magic;
        f >> magic;
        if (magic != "P5") throw std::runtime_error("仅支持二进制 PGM(P5), 实际: " + magic);
        PgmImage img;
        f >> img.width >> img.height;
        int maxval = 0;
        f >> maxval;
        f.get();  // 跳过 maxval 后的单个换行符
        if (img.width <= 0 || img.height <= 0 || maxval != 255)
            throw std::runtime_error("PGM 头非法");
        img.pixels.resize(static_cast<size_t>(img.width) * img.height);
        f.read(reinterpret_cast<char*>(img.pixels.data()),
               static_cast<std::streamsize>(img.pixels.size()));
        if (f.gcount() != static_cast<std::streamsize>(img.pixels.size()))
            throw std::runtime_error("像素数据不完整");
        return img;
    }

    const uint8_t* data() const { return pixels.data(); }        // 只读指针视图
    uint8_t at(int x, int y) const { return pixels[y * width + x]; }  // 行主序指针运算

    std::vector<uint8_t> pixels;

    void save(const std::string& path) const {
        std::ofstream f(path, std::ios::binary);
        f << "P5\n" << width << ' ' << height << "\n255\n";
        f.write(reinterpret_cast<const char*>(pixels.data()),
                static_cast<std::streamsize>(pixels.size()));
    }
};

// ---------- Otsu 全局阈值: 类间方差最大化 ----------
// hist[256] 用裸数组传入(演示与 C 风格 API/指针交互), 其余用 STL。
static int otsu_threshold(const uint32_t hist[256], int total) {
    double sum = 0.0;
    for (int i = 0; i < 256; ++i) sum += static_cast<double>(i) * hist[i];
    double sumB = 0.0, maxVar = 0.0;
    int wB = 0, best = 0;
    for (int i = 0; i < 256; ++i) {
        wB += hist[i];
        if (wB == 0) continue;
        const int wF = total - wB;
        if (wF == 0) break;
        sumB += static_cast<double>(i) * hist[i];
        const double mB = sumB / wB, mF = (sum - sumB) / wF;
        const double var = static_cast<double>(wB) * wF * (mB - mF) * (mB - mF);
        if (var > maxVar) { maxVar = var; best = i; }
    }
    return best;
}

int main(int argc, char* argv[]) {
    try {
        if (argc != 3) {
            std::cerr << "用法: " << argv[0] << " <input.pgm> <output.pgm>\n";
            return 2;
        }
        const auto loaded = PgmImage::load(argv[1]);
        auto img = std::make_unique<PgmImage>(std::move(loaded));  // 独占所有权(RAII)
        const int total = img->width * img->height;

        uint32_t hist[256] = {0};
        for (int i = 0; i < total; ++i) hist[img->data()[i]]++;  // 指针遍历

        const int thr = otsu_threshold(hist, total);
        std::cout << "尺寸: " << img->width << 'x' << img->height
                  << "  总像素: " << total << "  Otsu阈值: " << thr << '\n';

        for (int i = 0; i < total; ++i)
            img->pixels[i] = (img->pixels[i] > thr) ? 255 : 0;  // 二值化(就地改写)

        img->save(argv[2]);
        std::cout << "已写出: " << argv[2] << '\n';
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "错误: " << e.what() << '\n';
        return 1;
    }
}

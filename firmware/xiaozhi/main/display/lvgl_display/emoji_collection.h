#ifndef EMOJI_COLLECTION_H
#define EMOJI_COLLECTION_H

#include "lvgl_image.h"

#include <lvgl.h>

#include <map>
#include <string>
#include <memory>
#include <vector>


// Define interface for emoji collection
class EmojiCollection {
public:
    virtual void AddEmoji(const std::string& name, LvglImage* image);
    virtual const LvglImage* GetEmojiImage(const char* name);
    /* 已注册的表情名列表。给「防烧屏轮播」用：空闲时随机挑一个换上去，
     * 避免屏幕长期停在同一张图（IPS 也会 image sticking）。
     * 返回拷贝而不是引用：调用方常在外层持锁的场合遍历，拷贝最省心。 */
    virtual std::vector<std::string> EnumNames() const;
    virtual ~EmojiCollection();

private:
    std::map<std::string, LvglImage*> emoji_collection_;
};

class Twemoji32 : public EmojiCollection {
public:
    Twemoji32();
};

class Twemoji64 : public EmojiCollection {
public:
    Twemoji64();
};

#endif

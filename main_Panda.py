from direct.showbase.ShowBase import ShowBase
from panda3d.core import AmbientLight, DirectionalLight
import simplepbr


class Biorobot(ShowBase):

    def __init__(self):
        super().__init__()

        # Включаем PBR
        self.pipeline = simplepbr.init()

        # Отключаем управление мышью Panda3D
        self.disableMouse()

        print("\n==============================")
        print("Загрузка модели...")
        print("==============================")

        # Загружаем модель
        self.ani = self.loader.loadModel("assets/models/ani.glb")

        print("\nСтруктура модели:")
        self.ani.ls()

        print("\nТекстуры модели:")

        textures = self.ani.findAllTextures()

        if len(textures) == 0:
            print("❌ Panda3D НЕ нашёл ни одной текстуры.")
        else:
            for tex in textures:
                print(tex)

        print("\n==============================")

        self.ani.reparentTo(self.render)

        # Масштаб
        self.ani.setScale(10)

        # Положение
        self.ani.setPos(0, 8, -2)

        # Поворот
        self.ani.setH(180)

        # Камера
        self.cam.setPos(0, -25, 5)
        self.cam.lookAt(self.ani)

        # Свет

        ambient = AmbientLight("ambient")
        ambient.setColor((1, 1, 1, 1))
        ambient_np = self.render.attachNewNode(ambient)
        self.render.setLight(ambient_np)

        sun = DirectionalLight("sun")
        sun.setColor((3, 3, 3, 1))
        sun_np = self.render.attachNewNode(sun)
        sun_np.setHpr(-45, -35, 0)
        self.render.setLight(sun_np)


app = Biorobot()
app.run()
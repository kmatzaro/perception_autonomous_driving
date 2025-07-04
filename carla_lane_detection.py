import pygame
import numpy as np
import sys, glob, os
import cv2
from threading import Thread
from simple_lane_detection import SimpleLaneDetector
from validation_lane_detection import LaneValidator
import random
import datetime

try:
    sys.path.append(glob.glob('../carla/PythonAPI/carla/dist/carla-*%d.%d-%s.egg' % (
        sys.version_info.major,
        sys.version_info.minor,
        'win-amd64' if os.name == 'nt' else 'linux-x86_64'))[0])
except IndexError:
    pass

import carla


class CarlaLaneDetection:
    def __init__(self, enable_recording=False, town = 'Town03', validation_mode = True):
        self.client = None
        self.world = None
        self.town = town
        self.camera = None
        self.vehicle = None
        self.running = False
        self.actors = []
        self.lane_detector = SimpleLaneDetector((1280, 720))
        self.validation_mode = validation_mode
        self.enable_recording = enable_recording
        self.video_out = None
        self.current_frame = None  # Store current frame for main thread
        
        if self.enable_recording:
            self.init_video_writer()
        
        if self.validation_mode:
            self.frame_id = 0
            self.capture_times = 0.0
            self.logs = []
            self.sim_time = None
        
        # Display settings
        pygame.init()
        self.display = pygame.display.set_mode(self.lane_detector.img_size)
        pygame.display.set_caption("CARLA Lane Detection")

    def init_video_writer(self):
        """Initialize video writer"""
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_name = f"lane_detection_{timestamp}.mp4"
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        self.video_out = cv2.VideoWriter(output_name, fourcc, 30.0, self.lane_detector.img_size)

    def carla_setup(self):
        """Initialize CARLA connection and spawn vehicle"""
        try:
            # Connect to CARLA server
            self.client = carla.Client("localhost", 2000)
            self.client.set_timeout(10.0)
            self.world = self.client.load_world(self.town)
            blueprint_library = self.world.get_blueprint_library()

            # Configure synchronous mode
            settings = self.world.get_settings()
            settings.synchronous_mode = True
            settings.fixed_delta_seconds = 1.0 / 20  # denominator is the number of fps to run
            self.world.apply_settings(settings)

            # Set traffic manager to synchronous mode
            traffic_manager = self.client.get_trafficmanager()
            traffic_manager.set_synchronous_mode(True)
            print(f"Synchronous mode enabled at {1.0/self.world.get_settings().fixed_delta_seconds} FPS")

            # FIXED: Spawn vehicle FIRST, then camera
            spawn_points = self.world.get_map().get_spawn_points()
            vehicle_bp = blueprint_library.filter('vehicle.tesla.model3')[0]
            self.vehicle = self.world.spawn_actor(vehicle_bp, random.choice(spawn_points))
            self.vehicle.set_autopilot(True)
            self.actors.append(self.vehicle)

            # Setup camera AFTER vehicle is spawned
            camera_bp = blueprint_library.find('sensor.camera.rgb')
            camera_bp.set_attribute('image_size_x', '1280')
            camera_bp.set_attribute('image_size_y', '720')
            camera_bp.set_attribute('fov', '90')
            
            # Attach camera to vehicle
            camera_transform = carla.Transform(
                carla.Location(x=2.0, z=1.3),  # Position on vehicle
                carla.Rotation(pitch=-8)      # Slight downward angle
            )

            self.camera = self.world.spawn_actor(
                camera_bp, 
                camera_transform, 
                attach_to=self.vehicle
            )
            self.actors.append(self.camera)

            # FIXED: Set up camera callback properly (no separate thread needed)
            self.camera.listen(lambda image: self.camera_callback(image))

            if self.validation_mode:
                self.validator = LaneValidator(self.world, self.camera, self.vehicle, self.lane_detector)
                self.sim_time = self.world.get_snapshot().timestamp.elapsed_seconds

            print("CARLA setup complete!")
            return True
            
        except Exception as e:
            print(f"Error setting up CARLA: {e}")
            return False

    def camera_callback(self, image):
        """Process camera images (called from CARLA's thread)"""
        try:
            # Convert CARLA image to numpy array
            array = np.frombuffer(image.raw_data, dtype=np.uint8)
            array = array.reshape((image.height, image.width, 4))
            frame = array[:, :, :3]  # Remove alpha channel
                        
            # CARLA images are in BGRA format, convert to RGB
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            
            # Process with lane detector
            result, gray, edges, masked = self.lane_detector.process_image(frame)
            
            # Store processed frame for main thread to display
            self.current_frame = {
                'result': result,
                'gray': gray,
                'edges': edges,
                'masked': masked
            }
            
        except Exception as e:
            print(f"Error in camera callback: {e}")

    def update_display(self):
        """Update pygame display with current frame"""
        if self.current_frame is None:
            return
            
        try:
            result = self.current_frame['result']
            gray = self.current_frame['gray']
            edges = self.current_frame['edges']
            masked = self.current_frame['masked']
            
            # Result is already in RGB format from lane detector
            surface = pygame.surfarray.make_surface(np.rot90(result))
            self.display.blit(surface, (0, 0))

            # Record video if enabled (convert back to BGR for OpenCV)
            if self.enable_recording and self.video_out is not None:
                bgr_frame = cv2.cvtColor(result, cv2.COLOR_RGB2BGR)
                self.video_out.write(bgr_frame)

            # Create debug miniatures
            def draw_debug(title, img, y_offset):
                debug_img = cv2.resize(img, (160, 120))
                if len(debug_img.shape) == 2:  # Grayscale
                    debug_img = cv2.cvtColor(debug_img, cv2.COLOR_GRAY2RGB)
                debug_surface = pygame.surfarray.make_surface(np.rot90(debug_img))
                self.display.blit(debug_surface, (self.lane_detector.img_size[0]-200, y_offset))
                
                # Add text label
                font = pygame.font.SysFont(pygame.font.get_default_font(), 16)
                text = font.render(title, True, (255, 255, 255))
                self.display.blit(text, (self.lane_detector.img_size[0]-200, y_offset + 120))

            draw_debug("Gray", gray, 20)
            draw_debug("Edges", edges, 160)
            draw_debug("Masked", masked, 300)

            pygame.display.flip()  # Use flip() instead of update() for better performance
            
        except Exception as e:
            print(f"Error updating display: {e}")

    def run(self):
        """Main execution loop with synchronous mode"""
        if not self.carla_setup():
            print("Failed to setup CARLA")
            return
        
        self.running = True
        
        # Enable autopilot
        self.vehicle.set_autopilot(True)
        print("Autopilot enabled! Vehicle will drive automatically.")
        print("Press ESC to quit, SPACE to toggle autopilot on/off")
        print("Controls: W/S = throttle/brake, A/D = steer (when autopilot off)")
        print(f"Running in synchronous mode at {1.0/self.world.get_settings().fixed_delta_seconds} FPS")
        
        autopilot_enabled = True
        clock = pygame.time.Clock()  # Add clock for consistent timing
        
        try:
            # Main synchronous loop
            while self.running:
                # Tick the world to advance simulation
                self.world.tick()

                if self.validation_mode:
                    self.sim_time = self.world.get_snapshot().timestamp.elapsed_seconds
                    if self.sim_time > 5.0:
                        self.frame_id, self.capture_times, self.logs = self.validator.run_validation(self.sim_time, self.current_frame, self.frame_id, self.capture_times, self.logs)
                    
                
                # Handle pygame events
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        self.running = False
                    elif event.type == pygame.KEYDOWN:
                        if event.key == pygame.K_ESCAPE:
                            self.running = False
                        elif event.key == pygame.K_SPACE:
                            # Toggle autopilot
                            autopilot_enabled = not autopilot_enabled
                            self.vehicle.set_autopilot(autopilot_enabled)
                            status = "enabled" if autopilot_enabled else "disabled"
                            print(f"Autopilot {status}")
                
                # Manual control when autopilot is disabled
                if not autopilot_enabled:
                    keys = pygame.key.get_pressed()
                    throttle = 0.0
                    brake = 0.0
                    steer = 0.0
                    
                    if keys[pygame.K_w]:
                        throttle = 1
                    if keys[pygame.K_s]:
                        brake = 1
                    if keys[pygame.K_a]:
                        steer = 0.3
                    if keys[pygame.K_d]:
                        steer = -0.3
                    
                    control = carla.VehicleControl(
                        throttle=throttle,
                        brake=brake,
                        steer=steer
                    )
                    self.vehicle.apply_control(control)
                
                # Update display
                self.update_display()
                
                # Maintain consistent frame rate
                clock.tick(20)  # 20 FPS to match CARLA simulation
                
        except KeyboardInterrupt:
            print("Interrupted by user")
        finally:
            self.cleanup()
    
    def cleanup(self):
        """Clean up CARLA actors and connections"""
        print("Starting cleanup...")
        self.running = False
        
        if self.vehicle:
            self.vehicle.set_autopilot(False)  # Disable autopilot before cleanup
        
        # Clean up actors in reverse order
        for actor in self.actors:
            if actor is not None:
                try:
                    actor.destroy()
                except:
                    pass
        
        if self.video_out:
            self.video_out.release()
        
        # Restore asynchronous mode
        if self.world:
            try:
                settings = self.world.get_settings()
                settings.synchronous_mode = False
                settings.fixed_delta_seconds = None
                self.world.apply_settings(settings)
            except:
                pass
                
        pygame.quit()
        print("Cleanup complete - restored asynchronous mode")


if __name__ == '__main__':
    carla_lane_detection = CarlaLaneDetection(enable_recording=False, town='Town05')
    carla_lane_detection.run()
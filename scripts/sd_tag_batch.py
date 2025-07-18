import gradio as gr
import re
from modules import scripts, deepbooru, script_callbacks, shared
from modules.ui_components import InputAccordion
from modules.processing import process_images
from modules.shared import state
import sys
import importlib.util

NAME = "Img2img Batch Interrogator"

"""

Thanks to Mathias Russ.
Thanks to Smirking Kitsune.

"""

# references to main prompt components captured via on_after_component
img2img_prompt_comp = None
img2img_neg_prompt_comp = None

def _capture_prompt(component, **_kwargs):
    global img2img_prompt_comp
    if getattr(component, "elem_id", None) == "img2img_prompt":
        img2img_prompt_comp = component

def _capture_negative(component, **_kwargs):
    global img2img_neg_prompt_comp
    if getattr(component, "elem_id", None) == "img2img_neg_prompt":
        img2img_neg_prompt_comp = component

# register callbacks to capture prompt components once they are created
script_callbacks.on_after_component(_capture_prompt)
script_callbacks.on_after_component(_capture_negative)

# Extention List Crawler
def get_extensions_list():
    from modules import extensions
    extensions.list_extensions()
    ext_list = []
    for ext in extensions.extensions:
        ext: extensions.Extension
        ext.read_info_from_repo()
								  
        ext_list.append({
            "name": ext.name,
            "enabled": ext.enabled
        })
    return ext_list

# Extention Checker
def is_interrogator_enabled(interrogator):
    for ext in get_extensions_list():
        if ext["name"] == interrogator:
            return ext["enabled"]
    return False

# EXT Importer
def import_module(module_name, file_path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module

class InterrogationProcessor:
    wd_ext_utils = None
    clip_ext = None
    first = True
    # Mapping of tagger display names to their internal keys
    model_name_to_key = {}
    prompt_contamination = ""
		
    # Checks for CLIP EXT to see if it is installed and enabled
    @classmethod
    def load_clip_ext_module(cls):
        if is_interrogator_enabled('clip-interrogator-ext'):
            cls.clip_ext = import_module("clip-interrogator-ext", "extensions/clip-interrogator-ext/scripts/clip_interrogator_ext.py")
            print(f"[{NAME} LOADER]: `clip-interrogator-ext` found...")
            return cls.clip_ext
        print(f"[{NAME} LOADER]: `clip-interrogator-ext` NOT found!")
        return None

    # Initiates extenion check at startup for CLIP EXT
    @classmethod
    def load_clip_ext_module_wrapper(cls, *args, **kwargs):
        return cls.load_clip_ext_module()

    # Checks for WD EXT to see if it is installed and enabled
    @classmethod
    def load_wd_ext_module(cls):
        if is_interrogator_enabled('stable-diffusion-webui-wd14-tagger'):
            sys.path.append('extensions/stable-diffusion-webui-wd14-tagger')
            cls.wd_ext_utils = import_module("utils", "extensions/stable-diffusion-webui-wd14-tagger/tagger/utils.py")
            print(f"[{NAME} LOADER]: `stable-diffusion-webui-wd14-tagger` found...")
            return cls.wd_ext_utils
        print(f"[{NAME} LOADER]: `stable-diffusion-webui-wd14-tagger` NOT found!")
        return None
    
    # Initiates extenion check at startup for WD EXT
    @classmethod
    def load_wd_ext_module_wrapper(cls, *args, **kwargs):
        return cls.load_wd_ext_module()
        
    # Initiates prompt reset on image save
    @classmethod
    def load_custom_filter_module_wrapper(cls, *args, **kwargs):
        return cls.load_custom_filter()
    
    # Button interaction handler
    def b_clicked(o):
        return gr.Button.update(interactive=True)
    
    #Experimental Tool, prints dev statements to the console
    def debug_print(self, debug_mode, message):
        if debug_mode:
            print(f"[{NAME} DEBUG]: {message}")
    
    # Function to clean the custom_filter
    def clean_string(self, input_string):
        # Split the string into a list
        items = input_string.split(',')
        # Clean up each item: strip whitespace and convert to lowercase
        cleaned_items = [item.strip() for item in items if item.strip()]
        # Remove duplicates while preserving order
        unique_items = []
        seen = set()
        for item in cleaned_items:
            if item not in seen:
                seen.add(item)
                unique_items.append(item)
        # Join the cleaned, unique items back into a string
        return ', '.join(unique_items)
    
    # Custom replace function to replace phrases with associated pair
    def custom_replace(self, text, replace_pairs):
        for old, new in replace_pairs.items():
            text = re.sub(r'\b' + re.escape(old) + r'\b', new, text)
        return text

    # Tag filtering, removes negative tags from prompt
    def filter_words(self, prompt, negative):
        # Corrects a potential error where negative is nonetype
        if negative is None:
            negative = ""
        
        # Split prompt and negative strings into lists of words
        prompt_words = [word.strip() for word in prompt.split(",")]
        negative_words = [self.remove_attention(word.strip()) for word in negative.split(",")]
        
        # Filter out words from prompt that are in negative
        filtered_words = [word for word in prompt_words if self.remove_attention(word) not in negative_words]
        
        # Join filtered words back into a string
        filtered_prompt = ", ".join(filtered_words)
        
        return filtered_prompt

    # Initial Model Options generator, only add supported interrogators, support may vary depending on client
    def get_initial_model_options(self):
        options = ["CLIP (Native)", "Deepbooru (Native)"]
        if is_interrogator_enabled('clip-interrogator-ext'):
            options.insert(0, "CLIP (EXT)")
        if is_interrogator_enabled('stable-diffusion-webui-wd14-tagger'):
            options.append("WD (EXT)")
        return options
        
    # Gets a list of WD models from WD EXT
    def get_WD_EXT_models(self):
        if self.wd_ext_utils is not None:
            try:
                self.wd_ext_utils.refresh_interrogators()
                
                # Check if the mapping is empty and populate it if needed
                if not self.model_name_to_key:
                    print(f"[{NAME}]: Regenerating WD model mapping dictionary...")
                    
                # Create a dictionary mapping internal names to display names
                model_mapping = {}
                for key, interrogator in self.wd_ext_utils.interrogators.items():
                    # Use the display name if available, otherwise use the key
                    wd_model_display_name = interrogator.name if hasattr(interrogator, 'name') else key
                    model_mapping[key] = wd_model_display_name
                    # Also store the reverse mapping in the class variable
                    self.model_name_to_key[wd_model_display_name] = key
                
                if not model_mapping:
                    print(f"[{NAME}]: Warning: No WD Tagger models found.")
                else:
                    print(f"[{NAME}]: Found {len(model_mapping)} WD Tagger models.")
                    
                return model_mapping
            except Exception as error:
                print(f"[{NAME} ERROR]: Error accessing WD Tagger: {error}")
        return {}
    
    # Function to load CLIP models list into CLIP model selector
    def load_clip_models(self):
        if self.clip_ext is not None:
            models = self.clip_ext.get_models()
            return gr.Dropdown.update(choices=models if models else None)
        return gr.Dropdown.update(choices=None)
        
    # Function to load custom filter from file
    def load_custom_filter(self):
        try:
            with open("extensions/sd-Img2img-batch-interrogator/custom_filter.txt", "r", encoding="utf-8") as file:
                custom_filter = file.read()
                return custom_filter
        except Exception as error:
            print(f"[{NAME} ERROR]: Error loading custom filter: {error}")  
            # This should be resolved by generating a blank file
            if error == "[Errno 2] No such file or directory: 'extensions/sd-Img2img-batch-interrogator/custom_filter.txt'":
                self.save_custom_filter("")
            return ""
            
    # Function used to prep custom filter environment with previously saved configuration
    def load_custom_filter_on_start(self):
        return self.load_custom_filter()
    
    # Function to load custom replace from file
    def load_custom_replace(self):
        try:
            with open("extensions/sd-Img2img-batch-interrogator/custom_replace.txt", "r", encoding="utf-8") as file:
                content = file.read().strip().split('\n')
                if len(content) >= 2:
                    return content[0], content[1]
                else:
                    print(f"[{NAME} ERROR]: Invalid custom replace file format.")
                    return "", ""
        except Exception as error:
            print(f"[{NAME} ERROR]: Error loading custom replace: {error}")
            # This should be resolved by generating a blank file
            if error == "[Errno 2] No such file or directory: 'extensions/sd-Img2img-batch-interrogator/custom_replace.txt'":
                self.save_custom_replace("", "")
            return "", ""
    
    # Function used to prep find and replace environment with previously saved configuration
    def load_custom_replace_on_start(self):
        old, new = self.load_custom_replace()
        return old, new
	
    # Function to load keep tags from file
    def load_keep_tags(self):
        try:
            with open("extensions/sd-Img2img-batch-interrogator/keep_tags.txt", "r", encoding="utf-8") as file:
                keep_tags = file.read()
                return keep_tags
        except Exception as error:
            print(f"[{NAME} ERROR]: Error loading keep tags: {error}")  
            # This should be resolved by generating a blank file
            if error == "[Errno 2] No such file or directory: 'extensions/sd-Img2img-batch-interrogator/keep_tags.txt'":
                self.save_keep_tags("")
            return ""
    
    # Function used to prep keep tags environment with previously saved configuration
    def load_keep_tags_on_start(self):
        return self.load_keep_tags()
        
    # Function to load WD models list into WD model selector
    def load_wd_models(self):
        if self.wd_ext_utils is not None:
            model_mapping = self.get_WD_EXT_models()
            if model_mapping:
                # Create choices as tuples of (display_name, internal_name)
                choices = list(model_mapping.values())
                # Sort by display name for better user experience
                choices.sort()
                
                # Debug output to verify model mappings
                #print(f"[{NAME}]: Available WD models: {choices}")
                #print(f"[{NAME}]: Model name to key mapping: {self.model_name_to_key}")
                
                return gr.Dropdown.update(choices=choices)
        return gr.Dropdown.update(choices=[])
    
    # Parse two strings to display pairs
    def parse_replace_pairs(self, custom_replace_find, custom_replace_replacements):
        old_list = [phrase.strip() for phrase in custom_replace_find.split(',')]
        new_list = [phrase.strip() for phrase in custom_replace_replacements.split(',')]
        
        # Ensure both lists have the same length
        min_length = min(len(old_list), len(new_list))
        return {old_list[i]: new_list[i] for i in range(min_length)}
    
    # Refresh the model_selection dropdown
    def refresh_model_options(self):
        new_options = self.get_initial_model_options()
        return gr.Dropdown.update(choices=new_options)
    
    # Required to parse information from a string that is between () or has :##.## suffix
    def remove_attention(self, words):
        # Define a regular expression pattern to match attention-related suffixes
        pattern = r":\d+(\.\d+)?"
        # Remove attention-related suffixes using regex substitution
        words = re.sub(pattern, "", words)
        
        # Replace escaped left parenthesis with temporary placeholder
        words = re.sub(r"\\\(", r"TEMP_LEFT_PLACEHOLDER", words)
        # Replace escaped right parenthesis with temporary placeholder
        words = re.sub(r"\\\)", r"TEMP_RIGHT_PLACEHOLDER", words)
        # Define a regular expression pattern to match parentheses and their content
        pattern = r"(\(|\))"
        # Remove parentheses using regex substitution
        words = re.sub(pattern, "", words)
        # Restore escaped left parenthesis
        words = re.sub(r"TEMP_LEFT_PLACEHOLDER", r"\\(", words)
        # Restore escaped right parenthesis
        words = re.sub(r"TEMP_RIGHT_PLACEHOLDER", r"\\)", words)
        
        return words.strip()
    
    # Experimental Tool, removes puncutation, but tries to keep a variety of known emojis
    def remove_punctuation(self, text):
        # List of text emojis to preserve
        skipables = ["'s", "...", ":-)", ":)", ":-]", ":]", ":->", ":>", "8-)", "8)", ":-}", ":}", ":^)", "=]", "=)", ":-D", ":D", "8-D", "8D", "=D", "=3", "B^D", 
            "c:", "C:", "x-D", "X-D", ":-))", ":))", ":-(", ":(", ":-c", ":c", ":-<", ":<", ":-[", ":[", ":-||", ":{", ":@", ":(", ";(", ":'-(", ":'(", ":=(", ":'-)", 
            ":')", ">:(", ">:[", "D-':", "D:<", "D:", "D;", "D=", ":-O", ":O", ":-o", ":o", ":-0", ":0", "8-0", ">:O", "=O", "=o", "=0", ":-3", ":3", "=3", ">:3", 
            ":-*", ":*", ":x", ";-)", ";)", "*-)", "*)", ";-]", ";]", ";^)", ";>", ":-,", ";D", ";3", ":-P", ":P", "X-P", "x-p", ":-p", ":p", ":-Þ", ":Þ", ":-þ", 
            ":þ", ":-b", ":b", "d:", "=p", ">:P", ":-/", ":/", ":-.", ">:/", "=/", ":L", "=L", ":S", ":-|", ":|", ":$", "://)", "://3", ":-X", ":X", ":-#", ":#", 
            ":-&", ":&", "O:-)", "O:)", "0:-3", "0:3", "0:-)", "0:)", "0;^)", ">:-)", ">:)", "}:-)", "}:)", "3:-)", "3:)", ">;-)", ">;)", ">:3", ">;3", "|;-)", "|-O", 
            "B-)", ":-J", "#-)", "%-)", "%)", ":-###..", ":###..", "<:-|", "',:-|", "',:-l", ":E", "8-X", "8=X", "x-3", "x=3", "~:>", "@};-", "@}->--", "@}-;-'---", 
            "@>-->--", "8====D", "8===D", "8=D", "3=D", "8=>", "8===D~~~", "*<|:-)", "</3", "<\3", "<3", "><>", "<><", "<*)))-{", "><(((*>", "\o/", "*\0/*", "o7", 
            "v.v", "._.", "._.;", "X_X", "x_x", "+_+", "X_x", "x_X", "<_<", ">_>", "<.<", ">.>", "O_O", "o_o", "O-O", "o-o", "O_o", "o_O", ">.<", ">_<", "^5", "o/\o", 
            ">_>^ ^<_<", "V.v.V"] # Maybe I should remove emojis with parenthesis () in them...
        # Temporarily replace text emojis with placeholders
        for i, noticables in enumerate(skipables):
            text = text.replace(noticables, f"SKIP_PLACEHOLDER_{i}")
        # Remove punctuation except commas
        text = re.sub(r'[^\w\s,]', '', text)
        # Split the text into tags
        tags = [tag.strip() for tag in text.split(',')]
        # Remove empty tags
        tags = [tag for tag in tags if tag]
        # Rejoin the tags
        text = ', '.join(tags)
        # Restore text emojis
        for i, noticables in enumerate(skipables):
            text = text.replace(f"SKIP_PLACEHOLDER_{i}", noticables)
        return text
    
    # For WD Tagger, removes underscores from tags that should have spaces
    def replace_underscores(self, tag):
        skipable = [
            "0_0", "(o)_(o)", "+_+", "+_-", "._.", "<o>_<o>", "<|>_<|>", "=_=", ">_<", 
            "3_3", "6_9", ">_o", "@_@", "^_^", "o_o", "u_u", "x_x", "|_|", "||_||"
        ]
        if tag in skipable:
            return tag
        return tag.replace('_', ' ')

    # Resets the prompt_contamination string, prompt_contamination is used to clean the p.prompt after it has been modified by a previous batch job
    def reset_prompt_contamination(self, debug_mode):
        """
        Note: prompt_contamination
            During the course of a process_batch, the p.prompt and p.all_prompts[0] 
            is going to become contaminated with previous interrogation in the batch, to 
            mitigate this problem, prompt_contamination is used to identify and remove contamination
        """
        self.debug_print(debug_mode, f"Reset was Called! The following prompt will be removed from the prompt_contamination cleaner: {self.prompt_contamination}")
        self.prompt_contamination = ""
    
    # Function to save custom filter from file
    def save_custom_filter(self, custom_filter):
        try:
            with open("extensions/sd-Img2img-batch-interrogator/custom_filter.txt", "w", encoding="utf-8") as file:
                file.write(custom_filter)
                print(f"[{NAME}]: Custom filter saved successfully.")
        except Exception as error:
            print(f"[{NAME} ERROR]: Error saving custom filter: {error}")  
        return self.update_save_confirmation_row_false()

    # Function to save custom replace from file
    def save_custom_replace(self, custom_replace_find, custom_replace_replacements):
        try:
            with open("extensions/sd-Img2img-batch-interrogator/custom_replace.txt", "w", encoding="utf-8") as file:
                file.write(f"{custom_replace_find}\n{custom_replace_replacements}")
            print(f"[{NAME}]: Custom replace saved successfully.")
        except Exception as error:
            print(f"[{NAME} ERROR]: Error saving custom replace: {error}")
        return self.update_save_confirmation_row_false()
    
    # Function to save keep tags to file
    def save_keep_tags(self, keep_tags):
        try:
            with open("extensions/sd-Img2img-batch-interrogator/keep_tags.txt", "w", encoding="utf-8") as file:
                file.write(keep_tags)
                print(f"[{NAME}]: Keep tags saved successfully.")
        except Exception as error:
            print(f"[{NAME} ERROR]: Error saving keep tags: {error}")  
        return self.update_save_confirmation_row_false()				
	
    # depending on if CLIP (EXT) is present, CLIP (EXT) could be removed from model selector
    def update_clip_ext_visibility(self, model_selection):
        is_visible = "CLIP (EXT)" in model_selection
        if is_visible:
            clip_models = self.load_clip_models()
            try:
                return gr.update(visible=True), clip_models
            except:
                return gr.Accordion.update(visible=True), clip_models
        else:
            try:
                return gr.update(visible=False), gr.update()
            except:
                return gr.Accordion.update(visible=False), gr.Dropdown.update()
    
    # Updates the visibility of group with input bool making it dynamically visible
    def update_group_visibility(self, user_defined_visibility):
        try:
            return gr.update(visible=user_defined_visibility)
        except:
            return gr.Group.update(visible=user_defined_visibility)
    
    # Updates the visibility of slider with input bool making it dynamically visible
    def update_slider_visibility(self, user_defined_visibility):
        try:
            return gr.update(visible=user_defined_visibility)
        except:
            return gr.Slider.update(visible=user_defined_visibility)
    
    # Makes save confirmation dialague invisible
    def update_save_confirmation_row_false(self):
        try:
            return gr.update(visible=False)
        except:
            return gr.Accordion.update(visible=False)
    
    # Makes save confirmation dialague visible
    def update_save_confirmation_row_true(self):
        try:  
            return gr.update(visible=True)
        except:
            return gr.Accordion.update(visible=True)
    
    # Used for user visualization, (no longer used for parsing pairs)
    def update_parsed_pairs(self, custom_replace_find, custom_replace_replacements):
        old_list = [phrase.strip() for phrase in custom_replace_find.split(',')]
        new_list = [phrase.strip() for phrase in custom_replace_replacements.split(',')]
        
        # Ensure both lists have the same length
        min_length = min(len(old_list), len(new_list))
        pairs = [f"{old_list[i]}:{new_list[i]}" for i in range(min_length)]
        
        return ", ".join(pairs)

    def can_insert_at_index(self):
        """Return True if UI components were captured for index insertion."""
        return img2img_prompt_comp is not None and img2img_neg_prompt_comp is not None

    def update_insert_visibility(self, mode):
        if not self.can_insert_at_index():
            return [gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)]

        visible = mode == "Insert at index"
        return [gr.update(visible=visible), gr.update(visible=visible), gr.update(visible=visible)]

    def update_insert_preview(self, prompt, negative_prompt, target, index, mode):
        if not self.can_insert_at_index() or mode != "Insert at index":
            return gr.HighlightedText.update(visible=False), gr.Slider.update()

        text = prompt if target == "Prompt" else negative_prompt
        parts = [p.strip() for p in text.split(',') if p.strip()]
        try:
            idx = int(index)
        except Exception:
            idx = 0
        idx = max(0, min(idx, len(parts)))
        highlights = []
        open_attention = 0

        for p in parts:
            label = None
            if re.search(r"<[^>]+>", p):
                label = "lora"
            if re.search(r"[\(\[]", p):
                open_attention += 1
            if (
                open_attention > 0
                or re.search(r"[\)\]]", p)
                or re.search(r":\d", p)
            ):
                label = label or "attention"
            if re.search(r"[\)\]]", p) and open_attention > 0:
                open_attention -= len(re.findall(r"[\)\]]", p))

            highlights.append((p, label))

        highlights.insert(idx, ("<interrogation>", "insert"))
        return (
            gr.HighlightedText.update(value=highlights, visible=True),
            gr.Slider.update(maximum=len(parts), value=idx),
        )
    
    # depending on if WD (EXT) is present, WD (EXT) could be removed from model selector
    def update_wd_ext_visibility(self, model_selection):
        is_visible = "WD (EXT)" in model_selection
        if is_visible:
            wd_models = self.load_wd_models()
            try:
                return gr.update(visible=True), wd_models
            except:
                return gr.Accordion.update(visible=True), wd_models
        else:
            try:
                return gr.update(visible=False), gr.update()
            except:
                return gr.Accordion.update(visible=False), gr.Dropdown.update()
    
    #Unloads CLIP Models
    def unload_clip_models(self):
        if self.clip_ext is not None:
            self.clip_ext.unload()

    #Unloads WD Models
    def unload_wd_models(self):
        unloaded_models = 0
        if self.wd_ext_utils is not None:
            for interrogator in self.wd_ext_utils.interrogators.values():
                if interrogator.unload(): 
                    unloaded_models = unloaded_models + 1
            print(f"Unloaded {unloaded_models} Tagger Model(s).")
    
    def ui(self, is_img2img, skip_check=False):
        if not is_img2img and not skip_check:
            return []

        with InputAccordion(False, label=NAME, elem_id="tag_batch_enabled") as tag_batch_enabled:
            with gr.Row():
                model_selection = gr.Dropdown(
                    choices=self.get_initial_model_options(), 
                    label="Interrogation Model(s):",
                    multiselect=True
                )
                refresh_models_button = gr.Button("🔄", elem_classes="tool")
            
            insert_at_index_enabled = self.can_insert_at_index() and not skip_check

            in_front_choices = ["Prepend to prompt", "Append to prompt"]
            if insert_at_index_enabled:
                in_front_choices.append("Insert at index")

            in_front = gr.Radio(
                choices=in_front_choices,
                value="Prepend to prompt",
                label="Interrogator result position")

            insert_target = gr.Radio(
                choices=["Prompt", "Negative prompt"],
                value="Prompt",
                label="Insert into",
                visible=False)
            insert_index = gr.Slider(0, 100, value=0, step=1, label="Insert index", visible=False)
            insert_preview = gr.HighlightedText(label="Insertion preview", visible=False)
                        
            # CLIP EXT Options
            clip_ext_accordion = gr.Accordion("CLIP EXT Options:", visible=False)
            with clip_ext_accordion:
                clip_ext_model = gr.Dropdown(choices=[], value='ViT-L-14/openai', label="CLIP Extension Model(s):", multiselect=True)
                clip_ext_mode = gr.Radio(choices=["best", "fast", "classic", "negative"], value='best', label="CLIP Extension Mode")
                unload_clip_models_afterwords = gr.Checkbox(label="Unload CLIP Interrogator After Use", value=True)
                unload_clip_models_button = gr.Button(value="Unload All CLIP Interrogators")
                
            # WD EXT Options
            wd_ext_accordion = gr.Accordion("WD EXT Options:", visible=False)
            with wd_ext_accordion:
                wd_ext_model = gr.Dropdown(
                    choices=[], 
                    value=None,  # Will be set dynamically based on available models
                    label="WD Extension Model(s):", 
                    multiselect=True
                )
                wd_threshold = gr.Slider(0.0, 1.0, value=0.35, step=0.01, label="Tag Sensitivity Threshold")
                wd_underscore_fix = gr.Checkbox(label="Remove Underscores from Tags", value=True)
                wd_append_ratings = gr.Checkbox(label="Append Interpreted Rating(s)", value=False)
                wd_ratings = gr.Slider(0.0, 1.0, value=0.5, step=0.01, label="Rating(s) Sensitivity Threshold", visible=False)
                # WD Keep Tags feature, used to override confidence threshold to keep specified tag, even when confidence is below wd_threshold
                wd_keep_tags = gr.Textbox(
                    value=self.load_keep_tags_on_start(),
                    label="Keep Tags",
                    placeholder="Enter tags to always keep, separated by commas",
                    info="Tags listed here will always be included in the output regardless of threshold",
                    show_copy_button=True
                )
                # Button to clean up Keep Tags
                clean_keep_tags_button = gr.Button(value="Optimize Keep Tags")
                # Buttons to save/load keep tags
                with gr.Row():
                    load_keep_tags_button = gr.Button(value="Load Keep Tags")
                    save_keep_tags_button = gr.Button(value="Save Keep Tags")
                save_confirmation_keep_tags = gr.Accordion("Are You Sure You Want to Save?", visible=False)
                with save_confirmation_keep_tags:
                    with gr.Row():
                        cancel_save_keep_tags_button = gr.Button(value="Cancel")
                        confirm_save_keep_tags_button = gr.Button(value="Save", variant="stop")
                
                unload_wd_models_afterwords = gr.Checkbox(label="Unload Tagger After Use", value=True)
                unload_wd_models_button = gr.Button(value="Unload All Tagger Models")
                    
            filtering_tools = gr.Accordion("Filtering tools:")
            with filtering_tools:
                use_positive_filter = gr.Checkbox(label="Filter Duplicate Positive Prompt Entries from Interrogation")
                use_negative_filter = gr.Checkbox(label="Filter Duplicate Negative Prompt Entries from Interrogation")
                use_custom_filter = gr.Checkbox(label="Filter Custom Prompt Entries from Interrogation")
                custom_filter_group = gr.Group(visible=False)
                with custom_filter_group:
                    custom_filter = gr.Textbox(value=self.load_custom_filter_on_start(),
                        label="Custom Filter Prompt",
                        placeholder="Prompt content separated by commas. Warning ignores attention syntax, parentheses '()' and colon suffix ':XX.XX' are discarded.",
                        show_copy_button=True                        
                    )
                    # Button to remove duplicates and strip strange spacing
                    clean_custom_filter_button = gr.Button(value="Optimize Custom Filter")
                    # Button to load/save custom filter from file
                    with gr.Row():
                        load_custom_filter_button = gr.Button(value="Load Custom Filter")
                        save_custom_filter_button = gr.Button(value="Save Custom Filter")
                    save_confirmation_custom_filter = gr.Accordion("Are You Sure You Want to Save?", visible=False)
                    with save_confirmation_custom_filter:
                        with gr.Row():
                            cancel_save_custom_filter_button = gr.Button(value="Cancel")
                            confirm_save_custom_filter_button = gr.Button(value="Save", variant="stop")
                        
                # Find and Replace
                use_custom_replace = gr.Checkbox(label="Find & Replace User Defined Pairs in the Interrogation")
                custom_replace_group = gr.Group(visible=False)
                with custom_replace_group:
                    with gr.Row():
                        custom_replace_find = gr.Textbox(
                            value=self.load_custom_replace_on_start()[0],
                            label="Find:",
                            placeholder="Enter phrases to replace, separated by commas",
                            show_copy_button=True
                        )
                        custom_replace_replacements = gr.Textbox(
                            value=self.load_custom_replace_on_start()[1],
                            label="Replace:",
                            placeholder="Enter replacement phrases, separated by commas",
                            show_copy_button=True
                        )
                    with gr.Row():
                        parsed_pairs = gr.Textbox(
                            label="Parsed Pairs",
                            placeholder="Parsed pairs will be shown here",
                            interactive=False
                        )
                        update_parsed_pairs_button = gr.Button("🔄", elem_classes="tool")
                    with gr.Row():
                        load_custom_replace_button = gr.Button("Load Custom Replace")
                        save_custom_replace_button = gr.Button("Save Custom Replace")
                    save_confirmation_custom_replace = gr.Accordion("Are You Sure You Want to Save?", visible=False)
                    with save_confirmation_custom_replace:
                        with gr.Row():
                            cancel_save_custom_replace_button = gr.Button(value="Cancel")
                            confirm_save_custom_replace_button = gr.Button(value="Save", variant="stop")
                    
                
            experimental_tools = gr.Accordion("Experamental tools:", open=False)
            with experimental_tools:
                debug_mode = gr.Checkbox(label="Enable Debug Mode", info="[Debug Mode]: DEBUG statements will be printed to console log.")
                reverse_mode = gr.Checkbox(label="Enable Reverse Mode", info="[Reverse Mode]: Interrogation will be added to the negative prompt.")
                no_puncuation_mode = gr.Checkbox(label="Enable No Puncuation Mode", info="[No Puncuation Mode]: Interrogation will be filtered of all puncuations (except for a variety of emoji art).")
                exaggeration_mode = gr.Checkbox(label="Enable Exaggeration Mode", info="[Exaggeration Mode]: Interrogators will be permitted to add depulicate responses.")
                prompt_weight_mode = gr.Checkbox(label="Enable Interrogator Prompt Weight Mode", info="[Interrogator Prompt Weight]: Use attention syntax on interrogation.")
                prompt_weight = gr.Slider(0.0, 1.0, value=0.5, step=0.01, label="Interrogator Prompt Weight", visible=False) 
                prompt_output = gr.Checkbox(label="Enable Prompt Output", value=True, info="[Prompt Output]: Prompt statements will be printed to console log after every interrogation.")
                
            # Listeners
            model_selection.change(fn=self.update_clip_ext_visibility, inputs=[model_selection], outputs=[clip_ext_accordion, clip_ext_model])
            model_selection.change(fn=self.update_wd_ext_visibility, inputs=[model_selection], outputs=[wd_ext_accordion, wd_ext_model])
            unload_clip_models_button.click(self.unload_clip_models, inputs=None, outputs=None)
            unload_wd_models_button.click(self.unload_wd_models, inputs=None, outputs=None)
            prompt_weight_mode.change(fn=self.update_slider_visibility, inputs=[prompt_weight_mode], outputs=[prompt_weight])
            wd_append_ratings.change(fn=self.update_slider_visibility, inputs=[wd_append_ratings], outputs=[wd_ratings])
            clean_custom_filter_button.click(self.clean_string, inputs=custom_filter, outputs=custom_filter)
            load_custom_filter_button.click(self.load_custom_filter, inputs=None, outputs=custom_filter)
            clean_keep_tags_button.click(self.clean_string, inputs=wd_keep_tags, outputs=wd_keep_tags)
            load_keep_tags_button.click(self.load_keep_tags, inputs=None, outputs=wd_keep_tags)
            save_keep_tags_button.click(self.update_save_confirmation_row_true, inputs=None, outputs=[save_confirmation_keep_tags])
            cancel_save_keep_tags_button.click(self.update_save_confirmation_row_false, inputs=None, outputs=[save_confirmation_keep_tags])
            confirm_save_keep_tags_button.click(self.save_keep_tags, inputs=wd_keep_tags, outputs=[save_confirmation_keep_tags])
            custom_replace_find.change(fn=self.update_parsed_pairs, inputs=[custom_replace_find, custom_replace_replacements], outputs=[parsed_pairs])
            custom_replace_replacements.change(fn=self.update_parsed_pairs, inputs=[custom_replace_find, custom_replace_replacements], outputs=[parsed_pairs])
            update_parsed_pairs_button.click(fn=self.update_parsed_pairs, inputs=[custom_replace_find, custom_replace_replacements], outputs=[parsed_pairs])
            save_custom_filter_button.click(self.update_save_confirmation_row_true, inputs=None, outputs=[save_confirmation_custom_filter])
            cancel_save_custom_filter_button.click(self.update_save_confirmation_row_false, inputs=None, outputs=[save_confirmation_custom_filter])
            confirm_save_custom_filter_button.click(self.save_custom_filter, inputs=custom_filter, outputs=[save_confirmation_custom_filter])
            load_custom_replace_button.click(fn=self.load_custom_replace, inputs=[],outputs=[custom_replace_find, custom_replace_replacements])
            save_custom_replace_button.click(fn=self.update_save_confirmation_row_true, inputs=[], outputs=[save_confirmation_custom_replace])
            cancel_save_custom_replace_button.click(fn=self.update_save_confirmation_row_false, inputs=[], outputs=[save_confirmation_custom_replace])
            confirm_save_custom_replace_button.click(fn=self.save_custom_replace, inputs=[custom_replace_find, custom_replace_replacements], outputs=[save_confirmation_custom_replace])
            refresh_models_button.click(fn=self.refresh_model_options, inputs=[], outputs=[model_selection])
            use_custom_filter.change(fn=self.update_group_visibility, inputs=[use_custom_filter], outputs=[custom_filter_group])
            use_custom_replace.change(fn=self.update_group_visibility, inputs=[use_custom_replace], outputs=[custom_replace_group])
            in_front.change(fn=self.update_insert_visibility, inputs=[in_front], outputs=[insert_target, insert_index, insert_preview])

            if insert_at_index_enabled:
                in_front.change(
                    fn=self.update_insert_preview,
                    inputs=[img2img_prompt_comp, img2img_neg_prompt_comp, insert_target, insert_index, in_front],
                    outputs=[insert_preview, insert_index],
                )

                insert_target.change(
                    fn=self.update_insert_preview,
                    inputs=[img2img_prompt_comp, img2img_neg_prompt_comp, insert_target, insert_index, in_front],
                    outputs=[insert_preview, insert_index],
                )

                insert_index.change(
                    fn=self.update_insert_preview,
                    inputs=[img2img_prompt_comp, img2img_neg_prompt_comp, insert_target, insert_index, in_front],
                    outputs=[insert_preview, insert_index],
                )

            if insert_at_index_enabled and img2img_prompt_comp is not None:
                img2img_prompt_comp.change(
                    fn=self.update_insert_preview,
                    inputs=[img2img_prompt_comp, img2img_neg_prompt_comp, insert_target, insert_index, in_front],
                    outputs=[insert_preview, insert_index],
                )

            if insert_at_index_enabled and img2img_neg_prompt_comp is not None:
                img2img_neg_prompt_comp.change(
                    fn=self.update_insert_preview,
                    inputs=[img2img_prompt_comp, img2img_neg_prompt_comp, insert_target, insert_index, in_front],
                    outputs=[insert_preview, insert_index],
                )
                                    
        ui = [
            tag_batch_enabled, model_selection, debug_mode, in_front, insert_target, insert_index, prompt_weight_mode, prompt_weight, reverse_mode, exaggeration_mode, prompt_output, use_positive_filter, use_negative_filter,
            use_custom_filter, custom_filter, use_custom_replace, custom_replace_find, custom_replace_replacements, clip_ext_model, clip_ext_mode, wd_ext_model, wd_threshold, wd_underscore_fix, wd_append_ratings, wd_ratings, wd_keep_tags,
            unload_clip_models_afterwords, unload_wd_models_afterwords, no_puncuation_mode
            ]
        return ui

    def process_batch(
        self, p, tag_batch_enabled, model_selection, debug_mode, in_front, insert_target, insert_index, prompt_weight_mode, prompt_weight, reverse_mode, exaggeration_mode, prompt_output, use_positive_filter, use_negative_filter,
        use_custom_filter, custom_filter, use_custom_replace, custom_replace_find, custom_replace_replacements, clip_ext_model, clip_ext_mode, wd_ext_model, wd_threshold, wd_underscore_fix, wd_append_ratings, wd_ratings, wd_keep_tags,
        unload_clip_models_afterwords, unload_wd_models_afterwords, no_puncuation_mode, batch_number, prompts, seeds, subseeds,
        prompt_override=None, image_override=None, update_p=True):
            
        if not tag_batch_enabled:
            return None

        original_prompt = p.prompt
        original_negative = p.negative_prompt
        original_image = p.init_images[0] if p.init_images else None

        if prompt_override is not None:
            if reverse_mode:
                p.negative_prompt = prompt_override
            else:
                p.prompt = prompt_override

        if image_override is not None:
            p.init_images[0] = image_override
        
        self.debug_print(debug_mode, f"process_batch called. batch_number={batch_number}, state.job_no={state.job_no}, state.job_count={state.job_count}, state.job_count={state.job}")
        if model_selection and not batch_number:
            # Calls reset_prompt_contamination to prep for multiple p.prompts
            if state.job_no <= 0:
                self.debug_print(debug_mode, f"Condition met for reset, calling reset_prompt_contamination")
                self.reset_prompt_contamination(debug_mode)
            #self.debug_print(debug_mode, f"prompt_contamination: {self.prompt_contamination}")
            # Experimental reverse mode cleaner
            if not reverse_mode:
                # Remove contamination from previous batch job from negative prompt
                p.prompt = p.prompt.replace(self.prompt_contamination, "")
            else:
                # Remove contamination from previous batch job from negative prompt
                p.negative_prompt = p.prompt.replace(self.prompt_contamination, "")
            
            # local variable preperations
            self.debug_print(debug_mode, f"Initial p.prompt: {p.prompt}")
            preliminary_interrogation = ""
            interrogation = ""
            rating = {}
            
            # fix alpha channel
            init_image = p.init_images[0]
            p.init_images[0] = p.init_images[0].convert("RGB")
            
            # Interrogator interrogation loop
            for model in model_selection:
                # Check for skipped job
                if state.skipped:
                    print("Job skipped.")
                    state.skipped = False
                    continue
                    
                # Check for interruption
                if state.interrupted:
                    print("Job interrupted. Ending process.")
                    state.interrupted = False
                    break
                    
                # Should add the interrogators in the order determined by the model_selection list
                if model == "Deepbooru (Native)":
                    preliminary_interrogation = deepbooru.model.tag(p.init_images[0]) 
                    self.debug_print(debug_mode, f"[Deepbooru (Native)]: [Result]: {preliminary_interrogation}")
                    interrogation += f"{preliminary_interrogation}, "
                elif model == "CLIP (Native)":
                    preliminary_interrogation = shared.interrogator.interrogate(p.init_images[0]) 
                    self.debug_print(debug_mode, f"[CLIP (Native)]: [Result]: {preliminary_interrogation}")
                    interrogation += f"{preliminary_interrogation}, "
                elif model == "CLIP (EXT)":
                    if self.clip_ext is not None:
                        for clip_model in clip_ext_model:
                            # Clip-Ext resets state.job system during runtime...
                            job = state.job
                            job_no = state.job_no
                            job_count = state.job_count
                            # Check for skipped job
                            if state.skipped:
                                print("Job skipped.")
                                state.skipped = False
                                continue
                            # Check for interruption
                            if state.interrupted:
                                print("Job interrupted. Ending process.")
                                state.interrupted = False
                                break
                            preliminary_interrogation = self.clip_ext.image_to_prompt(p.init_images[0], clip_ext_mode, clip_model) 
                            if unload_clip_models_afterwords:
                                self.clip_ext.unload()
                            self.debug_print(debug_mode, f"[CLIP ({clip_model}:{clip_ext_mode})]: [Result]: {preliminary_interrogation}")
                            interrogation += f"{preliminary_interrogation}, "
                            # Redeclare variables for state.job system
                            state.job = job
                            state.job_no = job_no
                            state.job_count = job_count
                elif model == "WD (EXT)":
                    if self.wd_ext_utils is not None:
                        for wd_model_display_name in wd_ext_model:
                            # Check for skipped job
                            if state.skipped:
                                print("Job skipped.")
                                state.skipped = False
                                continue
                            # Check for interruption
                            if state.interrupted:
                                print("Job interrupted. Ending process.")
                                state.interrupted = False
                                break
                                
                            # Convert display name to internal key
                            internal_key = self.model_name_to_key.get(wd_model_display_name)
                            
                            # Debug logging for troubleshooting
                            self.debug_print(debug_mode, f"Using model display name: {wd_model_display_name}")
                            self.debug_print(debug_mode, f"Internal key mapped to: {internal_key}")
                            self.debug_print(debug_mode, f"Available model mappings: {self.model_name_to_key}")
                            self.debug_print(debug_mode, f"Available interrogators: {list(self.wd_ext_utils.interrogators.keys())}")
                            
                            # If the mapping is empty, try to regenerate it
                            if not internal_key and not self.model_name_to_key:
                                print(f"[{NAME}]: Model mapping is empty. Attempting to regenerate...")
                                self.get_WD_EXT_models()
                                internal_key = self.model_name_to_key.get(wd_model_display_name)
                            
                            # Fallback: if we still don't have an internal key, try using the display name directly
                            if not internal_key:
                                print(f"[{NAME}]: No internal key found for '{wd_model_display_name}'. Trying direct match...")
                                # Check if the display name exists directly in the interrogators
                                if wd_model_display_name in self.wd_ext_utils.interrogators:
                                    internal_key = wd_model_display_name
                                # Try case-insensitive match as last resort
                                else:
                                    for key in self.wd_ext_utils.interrogators.keys():
                                        if key.lower() == wd_model_display_name.lower():
                                            internal_key = key
                                            break
                            
                            #Failed State, will try to continue script gracefully
                            if internal_key is None:
                                print(f"[{NAME} ERROR]: No internal key found for display name '{wd_model_display_name}'")
                                print(f"Available mappings: {self.model_name_to_key}")
                                continue
                                
                            #Failed State, will try to continue script gracefully
                            if internal_key not in self.wd_ext_utils.interrogators:
                                print(f"[{NAME} ERROR]: Internal key '{internal_key}' not found in available interrogators")
                                print(f"Available interrogators: {list(self.wd_ext_utils.interrogators.keys())}")
                                continue
                                
                            # Use the internal key to access the interrogator
                            try:
                                rating, tags = self.wd_ext_utils.interrogators[internal_key].interrogate(p.init_images[0])
                                self.debug_print(debug_mode, f"Successfully interrogated using model: {wd_model_display_name} (internal key: {internal_key})")
                            except Exception as e:
                                print(f"[{NAME} ERROR]: Error interrogating with model '{wd_model_display_name}' (internal key: '{internal_key}'): {str(e)}")
                                continue
                                	
                            tags_list = [tag for tag, conf in tags.items() if conf > wd_threshold]
                            if wd_keep_tags:
                                for keep_tag in [t.strip() for t in wd_keep_tags.split(',') if t.strip()]:
                                    tag_key = keep_tag.replace(' ', '_')
                                    if tag_key in tags and tag_key not in tags_list:
                                        tags_list.append(tag_key)
                            if wd_underscore_fix:
                                tags_spaced = [self.replace_underscores(tag) for tag in tags_list]
                                preliminary_interrogation = ", ".join(tags_spaced)
                            else:
                                preliminary_interrogation = ", ".join(tags_list)
                            
                            if unload_wd_models_afterwords and internal_key in self.wd_ext_utils.interrogators:
                                self.wd_ext_utils.interrogators[internal_key].unload()
                                
                            self.debug_print(debug_mode, f"[WD ({wd_model_display_name}/{internal_key}:{wd_threshold})]: [Result]: {preliminary_interrogation}")
                            self.debug_print(debug_mode, f"[WD ({wd_model_display_name}/{internal_key}:{wd_threshold})]: [Ratings]: {rating}")
                            if wd_append_ratings:
                                qualifying_ratings = [key for key, value in rating.items() if value >= wd_ratings]
                                if qualifying_ratings:
                                    self.debug_print(wd_append_ratings, f"[WD ({wd_model_display_name}/{internal_key}:{wd_threshold})]: Rating sensitivity set to {wd_ratings}, therefore rating is: {qualifying_ratings}")
                                    preliminary_interrogation += ", " + ", ".join(qualifying_ratings)
                                else:
                                    self.debug_print(wd_append_ratings, f"[WD ({wd_model_display_name}/{internal_key}:{wd_threshold})]: Rating sensitivity set to {wd_ratings}, unable to determine a rating! Perhaps the rating sensitivity is set too high.")
                            interrogation += f"{preliminary_interrogation}, "
                            
            # Filter prevents overexaggeration of tags due to interrogation models having similar results 
            if not exaggeration_mode:
                interrogation = self.clean_string(interrogation)
            
            # Find and Replace user defined words in the interrogation prompt
            if use_custom_replace:
                replace_pairs = self.parse_replace_pairs(custom_replace_find, custom_replace_replacements)
                interrogation = self.custom_replace(interrogation, replace_pairs)
            
            # Remove duplicate prompt content from interrogator prompt
            if use_positive_filter:
                interrogation = self.filter_words(interrogation, p.prompt)
            # Remove negative prompt content from interrogator prompt
            if use_negative_filter:
                interrogation = self.filter_words(interrogation, p.negative_prompt)
            # Remove custom prompt content from interrogator prompt
            if use_custom_filter:
                interrogation = self.filter_words(interrogation, custom_filter)

            # Experimental tool for removing puncuations, but commas and a variety of emojis
            if no_puncuation_mode:
                interrogation = self.remove_punctuation(interrogation)
            
            # This will weight the interrogation, and also ensure that trailing commas to the interrogation are correctly placed.
            if prompt_weight_mode:
                interrogation = f"({interrogation.rstrip(', ')}:{prompt_weight}), "
            else:
                interrogation = f"{interrogation.rstrip(', ')}, "
            
            # Experimental reverse mode prep
            if not reverse_mode:
                prompt = p.prompt
            else:
                prompt = p.negative_prompt
            
            # This will construct the prompt
            if prompt == "":
                prompt = interrogation
            elif in_front == "Append to prompt":
                prompt = f"{prompt.rstrip(', ')}, {interrogation}"
            elif in_front == "Insert at index" and self.can_insert_at_index():
                base = p.prompt if insert_target == "Prompt" else p.negative_prompt
                parts = [x.strip() for x in base.split(',') if x.strip()]
                try:
                    idx = int(insert_index)
                except Exception:
                    idx = 0
                idx = max(0, min(idx, len(parts)))
                new_prompt = ", ".join(parts[:idx] + [interrogation.rstrip(', ')] + parts[idx:])
                if insert_target == "Prompt":
                    prompt = new_prompt
                else:
                    prompt = p.prompt
                    p.negative_prompt = new_prompt
            else:
                prompt = f"{interrogation}{prompt}"
            
            # Experimental reverse mode assignment
            if not reverse_mode:
                """
                Note: p.prompt, p.all_prompts[0], and prompts[0]
                    To get A1111 to record the updated prompt, p.all_prompts needs to be updated.
                    But, in process_batch to update the stable diffusion prompt, prompts[0] needs to be updated.
                    prompts[0] are already parsed for extra network syntax,
                """
                p.prompt = prompt
                for i in range(len(p.all_prompts)):
                    p.all_prompts[i] = prompt
                for i in range(len(prompts)):
                    prompts[i] = re.sub("[<].*[>]", "", prompt)
                if in_front == "Insert at index" and self.can_insert_at_index() and insert_target == "Negative prompt":
                    for i in range(len(p.all_negative_prompts)):
                        p.all_negative_prompts[i] = p.negative_prompt
            else:
                p.negative_prompt = prompt
                for i in range(len(p.all_negative_prompts)):
                    p.all_negative_prompts[i] = prompt
                
            # Restore Alpha Channel
            p.init_images[0] = init_image
            
            # Prep for reset
            self.prompt_contamination = interrogation
            
            # Prompt Output default is True
            self.debug_print(prompt_output or debug_mode, f"[Prompt]: {prompt}")

            self.debug_print(debug_mode, f"End of {NAME} Process ({state.job_no+1}/{state.job_count})...")

            result_prompt = prompt

            interrogation_result = interrogation.rstrip(', ')
            p.extra_generation_params["\nImg2img batch interrogation result"] = interrogation_result

            if self.wd_ext_utils is not None:
                p.extra_generation_params["Img2img batch WD model"] = ", ".join(wd_ext_model) if wd_ext_model else None
                p.extra_generation_params["Img2img batch WD threshold"] = wd_threshold
                p.extra_generation_params["Img2img batch WD Ratings"] = rating if rating else None

            if self.clip_ext is not None:
                p.extra_generation_params["Img2img batch CLIP model"] = ", ".join(clip_ext_model) if clip_ext_model else None
                p.extra_generation_params["Img2img batch CLIP mode"] = clip_ext_mode

            if not update_p:
                p.prompt = original_prompt
                p.negative_prompt = original_negative
                if original_image is not None:
                    p.init_images[0] = original_image
                return result_prompt
        
#Startup Callbacks
script_callbacks.on_app_started(InterrogationProcessor.load_clip_ext_module_wrapper)
script_callbacks.on_app_started(InterrogationProcessor.load_wd_ext_module_wrapper)

# Global interrogation processor instance for reuse by other extensions
interrogation_processor = InterrogationProcessor()


class Script(scripts.ScriptBuiltinUI):
    def title(self):
        return NAME

    def show(self, is_img2img):
        return scripts.AlwaysVisible if is_img2img else False

    def ui(self, is_img2img):
        return interrogation_processor.ui(is_img2img)

    def process_batch(self, *args, **kwargs):
        return interrogation_processor.process_batch(*args, **kwargs)

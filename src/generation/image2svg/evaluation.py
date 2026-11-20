#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import json
import re
import time
import traceback
import asyncio
import aiofiles
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
from openai import AsyncOpenAI
from ...eval_util import setup_logger, extract_svg_from_response, encode_image_to_base64
from .set_metric import evaluate_generated_svg, calculate_batch_fid
from ...metrics.data_util import rasterize_svg

logger = setup_logger(name="imagesvg_gen", log_dir="../logs", log_filename="imagesvg_gen.log")

API_KEY = "your_api_key_here"  # Replace with your actual API key
BASE_URL = "your_base_url_here"  # Replace with your API base URL
AVAILABLE_MODELS = [ "Qwen2-72B-Instruct-AWQ", "gpt-4o",  "deepseekr1"]

async def generate_svg_from_api_async(prompt: str, image_path: str, model: str = "deepseekr1", semaphore: asyncio.Semaphore = None) -> Tuple[Optional[str], float, Optional[str]]:
    """
    Asynchronously generate SVG using API with multimodal input
    
    Args:
        prompt: Generation prompt or instructions
        image_path: Path to image file
        model: Model name to use
        semaphore: Semaphore for controlling concurrency
        
    Returns:
        Generated SVG code, generation time, and full response text
    """
    start_time = time.time()
    async_client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)
    try:
        # Use semaphore to control concurrency
        async with semaphore:
            retries = 0
            max_retries = 10
            
            while retries < max_retries:
                try:
                    # Encode image to base64
                    base64_image = encode_image_to_base64(image_path)
                    if not base64_image:
                        logger.error(f"Failed to encode image: {image_path}")
                        return None, time.time() - start_time, None
                    
                    system_prompt = (
                        '''You are a professional SVG designer with extensive experience in vector graphics creation. Please generate the corresponding SVG code based on the user's description and the provided image reference.
                        Provide your answer strictly in the following format: Answer:{SVG code}, providing
                        the complete fixed code only in the {SVG code} position, without adding any explanations,
                        comments, or other content.Important: Your SVG must ONLY include <path> elements with "fill" and "d" attributes. Do not use any other SVG elements or attributes,and must use exactly this opening tag:
                        <svg class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg">'''
                    )
 
                    user_prompt = f"Please generate an SVG code based on this description: {prompt} and the reference image I've provided."
                   
                    messages = [
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user", 
                            "content": [
                                {"type": "text", "text": user_prompt},
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                            ]
                        }
                    ]
                    
                    stream = await async_client.chat.completions.create(
                        model=model,
                        messages=messages,
                        stream=True,
                    )
                    response = ""
                    async for chunk in stream:
                        if chunk.choices[0].delta.content:
                            content = chunk.choices[0].delta.content
                            response += content  
                    execution_time = time.time() - start_time
                    
                    response_text = response
                    svg_code = extract_svg_from_response(response_text)
                    
                    return svg_code, execution_time, response_text
                    
                except Exception as e:
                    retries += 1
                    logger.error(f"API call failed: {e}, retry {retries}")
                    if retries >= max_retries:
                        logger.error(f"Exceeded maximum retries, returning error")
                        return None, time.time() - start_time, "request error"
                    await asyncio.sleep(10) 
    
    except Exception as e:
        logger.error(f"Exception during API call: {e}")
        return None, time.time() - start_time, None


async def process_single_sample_async(sample: Dict[str, Any], model: str, output_dir: str, semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    """
    Asynchronously process single sample
  
    """
    # Get question description, reference SVG and image path
    question = sample.get('question', [''])[0]  # Get first question
    reference_svg = sample.get('answer', '')
    image_path = sample.get('image', '')  # Get image path
    
    if not question:
        return {
            'error': 'Missing question description',
            'reference_svg': reference_svg,
            'image_path': image_path
        }
    
    if not reference_svg:
        return {
            'error': 'Missing reference SVG',
            'question': question,
            'image_path': image_path
        }
        
    if not image_path or not os.path.exists(image_path):
        return {
            'error': f'Invalid or non-existent image path: {image_path}',
            'question': question,
            'reference_svg': reference_svg
        }
    
    logger.info(f"Processing question: {question}, image path: {image_path}")
    
    # Ensure sample output directory exists
    sample_id = f"sample_{int(time.time() * 1000)}"
    sample_output_dir = os.path.join(output_dir, sample_id)
    os.makedirs(sample_output_dir, exist_ok=True)
    
    # Asynchronously generate SVG (pass image path and semaphore)
    generated_svg, execution_time, full_response = await generate_svg_from_api_async(
        question, 
        image_path, 
        model, 
        semaphore
    )
    
    if not generated_svg:
        return {
            'sample_id': sample_id,
            'question': question,
            'reference_svg': reference_svg,
            'image_path': image_path,
            'error': 'SVG generation failed',
            'execution_time': execution_time,
            'full_response': full_response
        }
    
    # Asynchronously save SVG files
    try:
        # Save original and generated SVG to subdirectory
        async with aiofiles.open(os.path.join(sample_output_dir, "reference.svg"), 'w', encoding='utf-8') as f:
            await f.write(reference_svg)
        
        async with aiofiles.open(os.path.join(sample_output_dir, "generated.svg"), 'w', encoding='utf-8') as f:
            await f.write(generated_svg)
    except Exception as e:
        logger.error(f"Failed to save SVG files: {e}")
    
    #evaluate generated SVG
    evaluation_result = evaluate_generated_svg(
        question, 
        reference_svg, 
        generated_svg, 
        sample_output_dir
    )
    
    # Return results
    result = {
        'sample_id': sample_id,
        'question': question,
        'reference_svg': reference_svg,
        'image_path': image_path,
        'generated_svg': generated_svg,
        'execution_time': execution_time,
        'full_response': full_response,
        'evaluation': evaluation_result
    }
    
    return result

async def process_samples_from_json_async(json_file: str, output_file: str, model: str = "deepseekr1", max_concurrent: int = 5, enable_fid: bool = True) -> Dict[str, Any]:
    """
    Asynchronously process samples from JSON file

    """
    # Load JSON data
    try:
        with open(json_file, 'r', encoding='utf-8') as f:
            samples_data = json.load(f)
            
        if not isinstance(samples_data, list):
            logger.error(f"JSON data must be in list format")
            return {"error": "Invalid JSON data format, should be list"}
            
        samples = samples_data
            
    except Exception as e:
        logger.error(f"Failed to load JSON file {json_file}: {e}")
        return {"error": f"Failed to load JSON file: {e}"}
    
    if not samples:
        return {"error": "Sample data is empty"}

    output_dir = os.path.dirname(os.path.abspath(output_file))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    evaluation_dir = os.path.join(output_dir, "evaluation_results")
    os.makedirs(evaluation_dir, exist_ok=True)
    
    semaphore = asyncio.Semaphore(max_concurrent)
    
    batch_size = max_concurrent * 2 
    total_samples = len(samples)
    processed = 0
    all_results = []
    
    for start_idx in range(0, total_samples, batch_size):
        end_idx = min(start_idx + batch_size, total_samples)
        batch_samples = samples[start_idx:end_idx]
        
        logger.info(f"Processing batch {start_idx//batch_size + 1}, samples {start_idx+1}-{end_idx}...")
   
        batch_tasks = [
            process_single_sample_async(sample, model, evaluation_dir, semaphore)
            for sample in batch_samples
        ]

        batch_results = await asyncio.gather(*batch_tasks)
        all_results.extend(batch_results)
        processed += len(batch_results)
       
        interim_output = f"{os.path.splitext(output_file)[0]}_interim_{processed}.json"
        with open(interim_output, 'w', encoding='utf-8') as f:
            json.dump({
                "processed": processed,
                "total": total_samples,
                "results": all_results
            }, f, indent=2, ensure_ascii=False)
        logger.info(f"Processed {processed}/{total_samples} samples, interim results saved to: {interim_output}")

    successful_samples = [r for r in all_results if "error" not in r]
    sample_count = len(successful_samples)
    fid_score = None
    if enable_fid and sample_count >= 2:
        logger.info("Starting FID score calculation...")
        reference_images = []
        generated_images = []
        
        for result in successful_samples:
            try:
                ref_svg = result.get('reference_svg', '')
                gen_svg = result.get('generated_svg', '')
                
                if ref_svg and gen_svg:
                    ref_img = rasterize_svg(ref_svg)
                    gen_img = rasterize_svg(gen_svg)
                    
                    if ref_img is not None and gen_img is not None:
                        reference_images.append(ref_img)
                        generated_images.append(gen_img)
            except Exception as e:
                logger.error(f"Failed to rasterize SVG: {e}")
        
        if len(reference_images) >= 2 and len(generated_images) >= 2:
            fid_score = calculate_batch_fid(reference_images, generated_images)
            logger.info(f"FID score calculation completed: {fid_score}")
        else:
            logger.warning(f"Insufficient valid rasterized images for FID calculation. Reference: {len(reference_images)}, Generated: {len(generated_images)}")
    
    metrics_summary = {
        "execution_time": {
            "total": 0.0,
            "min": float('inf'),
            "max": 0.0,
            "avg": 0.0
        },
        
        "path_level": {
            "final_reward": {"total": 0.0, "min": float('inf'), "max": 0.0, "avg": 0.0},
            "global_iou": {"total": 0.0, "min": float('inf'), "max": 0.0, "avg": 0.0},
            "shape": {"total": 0.0, "min": float('inf'), "max": 0.0, "avg": 0.0},
            "color": {"total": 0.0, "min": float('inf'), "max": 0.0, "avg": 0.0},
            "position": {"total": 0.0, "min": float('inf'), "max": 0.0, "avg": 0.0},
            "order": {"total": 0.0, "min": float('inf'), "max": 0.0, "avg": 0.0},
            "path_count": {"total": 0.0, "min": float('inf'), "max": 0.0, "avg": 0.0}
        },
        
        "image_metrics": {
            "SSIM": {"total": 0.0, "min": float('inf'), "max": 0.0, "avg": 0.0},
            "LPIPS": {"total": 0.0, "min": float('inf'), "max": 0.0, "avg": 0.0},
            "MSE": {"total": 0.0, "min": float('inf'), "max": 0.0, "avg": 0.0},
            "DinoScore": {"total": 0.0, "min": float('inf'), "max": 0.0, "avg": 0.0}
        }
    }
    
    if fid_score is not None:
        metrics_summary["image_metrics"]["FID"] = fid_score

    for result in successful_samples:
        execution_time = result.get('execution_time', 0)
        metrics_summary["execution_time"]["total"] += execution_time
        metrics_summary["execution_time"]["min"] = min(metrics_summary["execution_time"]["min"], execution_time)
        metrics_summary["execution_time"]["max"] = max(metrics_summary["execution_time"]["max"], execution_time)
        
        if 'evaluation' in result:
            evaluation = result['evaluation']
            
            if 'path_level_metrics' in evaluation:
                path_metrics = evaluation['path_level_metrics']
                
                if 'final_reward' in path_metrics:
                    value = path_metrics['final_reward']
                    metrics_summary["path_level"]["final_reward"]["total"] += value
                    metrics_summary["path_level"]["final_reward"]["min"] = min(metrics_summary["path_level"]["final_reward"]["min"], value)
                    metrics_summary["path_level"]["final_reward"]["max"] = max(metrics_summary["path_level"]["final_reward"]["max"], value)
                
                if 'dimension_scores' in path_metrics:
                    dim_scores = path_metrics['dimension_scores']
                   
                    for dim_name in ['global_iou', 'shape', 'color', 'position', 'order', 'path_count']:
                        if dim_name in dim_scores:
                            value = dim_scores[dim_name]
                            metrics_summary["path_level"][dim_name]["total"] += value
                            metrics_summary["path_level"][dim_name]["min"] = min(metrics_summary["path_level"][dim_name]["min"], value)
                            metrics_summary["path_level"][dim_name]["max"] = max(metrics_summary["path_level"][dim_name]["max"], value)
           
            if 'image_metrics' in evaluation:
                image_metrics = evaluation['image_metrics']
                
                for metric_name in ['SSIM', 'LPIPS', 'MSE', 'DinoScore']:
                    if metric_name in image_metrics:
                        value = image_metrics[metric_name]
                        metrics_summary["image_metrics"][metric_name]["total"] += value
                        metrics_summary["image_metrics"][metric_name]["min"] = min(metrics_summary["image_metrics"][metric_name]["min"], value)
                        metrics_summary["image_metrics"][metric_name]["max"] = max(metrics_summary["image_metrics"][metric_name]["max"], value)
   
    if sample_count > 0:
        metrics_summary["execution_time"]["avg"] = metrics_summary["execution_time"]["total"] / sample_count
        
        for metric_name in metrics_summary["path_level"]:
            metrics_summary["path_level"][metric_name]["avg"] = metrics_summary["path_level"][metric_name]["total"] / sample_count
            if metrics_summary["path_level"][metric_name]["min"] == float('inf'):
                metrics_summary["path_level"][metric_name]["min"] = 0.0
   
        for metric_name in metrics_summary["image_metrics"]:
            if metric_name != "FID" and isinstance(metrics_summary["image_metrics"][metric_name], dict):
                metrics_summary["image_metrics"][metric_name]["avg"] = metrics_summary["image_metrics"][metric_name]["total"] / sample_count
                if metrics_summary["image_metrics"][metric_name]["min"] == float('inf'):
                    metrics_summary["image_metrics"][metric_name]["min"] = 0.0

    summary = {
        "total_samples": len(samples_data),
        "successful_samples": sample_count,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "model": model,
        "metrics": metrics_summary
    }
   
    full_result = {
        "summary": summary,
        "results": all_results
    }

    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(full_result, f, indent=2, ensure_ascii=False)
        logger.info(f"Processing results saved to: {output_file}")
    except Exception as e:
        logger.error(f"Failed to save results to {output_file}: {e}")
    
    return summary

async def main_async():
    """
    Asynchronous main function
    """
    import argparse
    
    parser = argparse.ArgumentParser(description='SVG generation and evaluation')
    parser.add_argument('--input', type=str, required=True, help='Input JSON file path')
    parser.add_argument('--output', type=str, required=True, help='Output result JSON file path')
    parser.add_argument('--model', type=str, default="Qwen2.5-VL-72B-Instruct", help='Model name to use')
    parser.add_argument('--disable-fid', action='store_true', help='Disable FID evaluation')
    parser.add_argument('--concurrent', type=int, default=5, help='Maximum concurrent requests')
    
    args = parser.parse_args()
    
    try:
        # Asynchronously process samples with concurrency control
        summary = await process_samples_from_json_async(
            args.input, 
            args.output, 
            args.model,
            args.concurrent,
            enable_fid=not args.disable_fid
        )
        
        # Output summary results
        if "error" in summary:
            print(f"Processing failed: {summary['error']}")
        else:
            print(f"Processing completed, total {summary['total_samples']} samples, {summary['successful_samples']} successful")
            
            # if "metrics" in summary:
            #     # Print execution time
            #     print(f"\nAverage execution time: {summary['metrics']['execution_time']['avg']:.2f} seconds")
                
            #     # Print path-level metrics
            #     print("\nPath-level metrics:")
            #     print(f"  Final reward: {summary['metrics']['path_level']['final_reward']['avg']:.4f}")
            #     print(f"  Global IoU: {summary['metrics']['path_level']['global_iou']['avg']:.4f}")
            #     print(f"  Shape: {summary['metrics']['path_level']['shape']['avg']:.4f}")
            #     print(f"  Color: {summary['metrics']['path_level']['color']['avg']:.4f}")
            #     print(f"  Position: {summary['metrics']['path_level']['position']['avg']:.4f}")
            #     print(f"  Order: {summary['metrics']['path_level']['order']['avg']:.4f}")
            #     print(f"  Path count: {summary['metrics']['path_level']['path_count']['avg']:.4f}")
                
            #     # Print image-level metrics
            #     print("\nImage-level metrics:")
            #     print(f"  SSIM: {summary['metrics']['image_metrics']['SSIM']['avg']:.4f}")
            #     print(f"  LPIPS: {summary['metrics']['image_metrics']['LPIPS']['avg']:.4f}")
            #     print(f"  MSE: {summary['metrics']['image_metrics']['MSE']['avg']:.4f}")
            #     print(f"  DinoScore: {summary['metrics']['image_metrics']['DinoScore']['avg']:.4f}")
                
            #     # Print FID score if available
            #     if "FID" in summary['metrics']['image_metrics']:
            #         print(f"  FID: {summary['metrics']['image_metrics']['FID']:.4f}")
            
            print(f"\nDetailed results saved to: {args.output}")
    
    except Exception as e:
        print(f"Error occurred: {e}")
        print(traceback.format_exc())
        sys.exit(1)

def main():
    """
    Command line entry function, using async run
    """
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
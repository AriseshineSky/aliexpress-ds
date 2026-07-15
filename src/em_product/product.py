from pydantic import BaseModel, ValidationError, field_validator
from datetime import datetime
from typing import Optional, List, Dict, Literal, Union, Any
from checkdigit import isbn, gs1
import re


class BaseVariant(BaseModel):
    sku: str
    price: float
    currency: Optional[str] = "USD"
    available_qty: Optional[int] = None


class OptionValue(BaseModel):
    option_id: Optional[str] = None
    option_value_id: Optional[str] = None
    option_name: str
    option_value: str


class Variant(BaseVariant):
    barcode: Optional[str]
    variant_id: Optional[str]
    option_values: List[OptionValue]
    images: Optional[str]

    @field_validator("images")
    def validate_images(cls, images):
        if images is None:
            return images

        try:
            for image in images.split(";"):
                if not image.startswith("http"):
                    raise ValueError("images must be a string")
            return images
        except Exception as e:
            raise ValueError(str(e))


class ProductSource(BaseModel):
    date: Any
    url: str
    source: str
    existence: bool
    sku: Optional[str]
    price: Optional[float]
    available_qty: Optional[int]
    images: Optional[str]
    product_id: Optional[Union[str, int]]
    title: Optional[str]
    title_en: Optional[str] = None
    description: Optional[str]
    description_en: Optional[str] = None
    summary: Optional[str]
    upc: Optional[str]
    brand: Optional[str]
    specifications: Optional[List[Dict[str, str]]]
    categories: Optional[str]
    videos: Optional[str]
    options: Optional[List[Dict[Literal["name"], str]]]
    variants: Optional[List[Variant]]
    returnable: Optional[bool]
    reviews: Optional[int]
    rating: Optional[float]
    sold_count: Optional[int]
    shipping_fee: Optional[float]
    shipping_days_min: Optional[int]
    shipping_days_max: Optional[int]
    weight: Optional[float]
    width: Optional[float]
    height: Optional[float]
    length: Optional[float]
    has_only_default_variant: Optional[bool]


class OptionDict(BaseModel):
    name: str
    id: Optional[str] = None


class StandardProduct(BaseVariant):
    date: str
    url: str
    source: str
    images: str
    product_id: Union[str, int]
    existence: bool
    title: str
    title_en: Optional[str] = None
    description: Optional[str]
    description_en: Optional[str] = None
    summary: Optional[str] = None
    upc: Optional[str]
    brand: Optional[str]
    specifications: Optional[List[Dict[str, str]]]
    categories: Optional[str]
    videos: Optional[str] = None
    options: Optional[List[OptionDict]]
    variants: Optional[List[Variant]]
    returnable: Optional[bool] = None
    reviews: Optional[int] = None
    rating: Optional[float] = None
    sold_count: Optional[int] = None
    shipping_fee: float
    shipping_days_min: Optional[int]
    shipping_days_max: Optional[int]
    weight: Optional[float]
    width: Optional[float]
    height: Optional[float]
    length: Optional[float]
    has_only_default_variant: Optional[bool]
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @field_validator("date")
    def validate_date(cls, value):
        try:
            parsed_date = datetime.fromisoformat(value)
            format_date = parsed_date.replace(microsecond=0).isoformat()
        except ValueError:
            raise ValueError("date must be in ISO 8601 format and omit microseconds")
        return format_date

    @field_validator("specifications")
    def validate_specifications(cls, specs):
        if specs is None:
            return specs
        try:
            for spec in specs:
                spec_bk = spec.copy()
                spec_bk.pop("name")
                spec_bk.pop("value")
                if spec_bk:
                    raise ValueError(
                        "1. Specifications must be None or List.\n"
                        "2. specification must be like {name:name, value:value}"
                    )
        except Exception as e:
            raise ValueError(str(e))

        return specs

    @field_validator("url")
    def validate_url(cls, url):
        if url is None:
            return url

        try:
            if not url.startswith("https"):
                raise ValueError("url must be a string and startswith http")
        except Exception as e:
            raise ValueError(str(e))

        return url

    @field_validator("upc")
    def validate_upc(cls, upc):
        if upc is None:
            return upc

        try:
            if not gs1.validate(upc) and not isbn.validate(upc):
                upc = None
        except Exception as e:
            upc = None

        return upc

    @field_validator("images")
    def validate_images(cls, images):
        try:
            for image in images.split(";"):
                if not image.startswith("http"):
                    raise ValueError("images must be a string")
        except Exception as e:
            raise ValueError(str(e))

        return images

    @field_validator("categories")
    def validate_unique_categories(cls, categories):
        try:
            if categories is None:
                return categories
            category_set = set()
            for category in categories.split(">"):
                if category.strip() in category_set:
                    raise ValueError("category must be unique")
                category_set.add(category.strip())
        except Exception as e:
            raise ValueError(str(e))

        return categories

    @field_validator("description")
    def validate_description(cls, description):
        try:
            if not description:
                raise ValueError("Description can not be empty")
            if re.search(r"<a\s+[^>]*>", description, re.IGNORECASE):
                raise ValueError("There should be no a tag in description")
        except Exception as e:
            raise ValueError(str(e))

        return description

    @field_validator("weight")
    def validate_weight(cls, weight):
        if weight is None:
            return weight

        if weight > 2.5:
            raise ValueError("weight must be less than or equal to 2.5lb")

        return weight

    @field_validator("length")
    def validate_length(cls, length):
        if length is None:
            return length

        if length > 12:
            raise ValueError("length must be less than or equal to 12 inches")

        return length


class SourceProduct(BaseVariant):
    date: datetime
    url: str
    images: str
    product_id: Union[str, int]
    existence: bool
    title: str
    description: Optional[str]
    summary: Optional[str] = None
    upc: Optional[str]
    brand: Optional[str]
    specifications: Optional[List[Dict[str, str]]]
    categories: Optional[str]
    videos: Optional[str] = None
    options: Optional[List[OptionDict]]
    variants: Optional[List[Variant]]
    returnable: Optional[bool] = None
    reviews: Optional[int] = None
    rating: Optional[float] = None
    sold_count: Optional[int] = None
    shipping_fee: float
    shipping_days_min: Optional[int] = None
    shipping_days_max: Optional[int] = None
    weight: Optional[float]
    width: Optional[float]
    height: Optional[float]
    length: Optional[float]
    has_only_default_variant: Optional[bool]

    @field_validator("specifications")
    def validate_specifications(cls, specs):
        if specs is None:
            return specs
        try:
            for spec in specs:
                spec_bk = spec.copy()
                spec_bk.pop("name")
                spec_bk.pop("value")
                if spec_bk:
                    raise ValueError(
                        "1. Specifications must be None or List.\n"
                        "2. specification must be like {name:name, value:value}"
                    )
        except Exception as e:
            raise ValueError(str(e))

        return specs

    @field_validator("url")
    def validate_url(cls, url):
        if url is None:
            return url

        try:
            if not url.startswith("https"):
                raise ValueError("url must be a string and startswith http")
        except Exception as e:
            raise ValueError(str(e))

        return url

    @field_validator("upc")
    def validate_upc(cls, upc):
        if upc is None:
            return upc

        try:
            if not gs1.validate(upc) and not isbn.validate(upc):
                upc = None
        except Exception as e:
            upc = None

        return upc

    @field_validator("images")
    def validate_images(cls, images):
        try:
            for image in images.split(";"):
                if not image.startswith("http"):
                    raise ValueError("images must be a string")
        except Exception as e:
            raise ValueError(str(e))

        return images

    @field_validator("categories")
    def validate_unique_categories(cls, categories):
        try:
            if categories is None:
                return categories
            category_set = set()
            for category in categories.split(">"):
                if category.strip() in category_set:
                    raise ValueError("category must be unique")
                category_set.add(category.strip())
        except Exception as e:
            raise ValueError(str(e))

        return categories

    @field_validator("description")
    def validate_description(cls, description):
        try:
            if description is None:
                return description
            if re.search(r"<a\s+[^>]*>", description, re.IGNORECASE):
                raise ValueError("There should be no a tag in description")
        except Exception as e:
            raise ValueError(str(e))

        return description

    @field_validator("weight")
    def validate_weight(cls, weight):
        if weight is None:
            return weight

        if weight > 2.5:
            raise ValueError("weight must be less than or equal to 2.5lb")

        return weight

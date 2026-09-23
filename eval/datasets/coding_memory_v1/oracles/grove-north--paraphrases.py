"""Immutable oracle for grove-north:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-grove-north', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
